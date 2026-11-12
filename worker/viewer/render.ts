import * as THREE from "three";
import {OrbitControls} from "three/addons/controls/OrbitControls.js";
import {GLTFLoader} from "three/addons/loaders/GLTFLoader.js";
import {RoomEnvironment} from "three/addons/environments/RoomEnvironment.js";
import type {SparkRenderer, SplatMesh} from "@sparkjsdev/spark";

// Only self-contained GLBs are supported. Inspect before a loader can issue a
// request, including extensions that would otherwise dereference external URLs.
export function checkGLB(bytes: ArrayBuffer): void {
  const header = new DataView(bytes);
  if (bytes.byteLength < 20 || header.getUint32(0, true) !== 0x46546c67 ||
      header.getUint32(4, true) !== 2 || header.getUint32(8, true) !== bytes.byteLength ||
      header.getUint32(16, true) !== 0x4e4f534a) throw Error("GLBファイルが壊れています。");
  const length = header.getUint32(12, true);
  if (length > bytes.byteLength - 20) throw Error("GLBファイルが不完全です。");
  const json: unknown = JSON.parse(new TextDecoder().decode(new Uint8Array(bytes, 20, length)));
  const check = (value: unknown): void => {
    if (!value || typeof value !== "object") return;
    for (const [key, item] of Object.entries(value)) {
      if (key === "uri" && typeof item === "string" && !item.startsWith("data:"))
        throw Error("外部ファイルを参照するGLBには対応していません。");
      check(item);
    }
  };
  check(json);
}

export function createViewer(container: HTMLElement, onError: (error: unknown) => void) {
  const renderer = new THREE.WebGLRenderer({antialias: true});
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.domElement.tabIndex = 0;
  renderer.domElement.setAttribute("aria-label", "3Dモデル。ドラッグで回転、スクロールで拡大、矢印キーで移動。");
  container.replaceChildren(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x141a22);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.listenToKeyEvents(renderer.domElement);
  const bounds = new THREE.Box3();
  let disposed = false, frame = 0, model: THREE.Object3D | undefined;
  let spark: SparkRenderer | undefined, splats: SplatMesh | undefined;
  let environment: THREE.WebGLRenderTarget | undefined;

  function draw() {
    if (disposed || frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (disposed) return;
      try { renderer.render(scene, camera); } catch (error) { onError(error); }
    });
  }
  function reset() {
    if (bounds.isEmpty()) return;
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.01);
    const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.15 /
      Math.min(1, camera.aspect);
    camera.near = Math.max(radius / 1000, 0.00001);
    camera.far = distance + radius * 100;
    camera.position.copy(center).add(new THREE.Vector3(1, 0.6, 1.5).normalize().multiplyScalar(distance));
    controls.target.copy(center);
    controls.maxDistance = distance * 20;
    camera.updateProjectionMatrix();
    controls.update();
    draw();
  }
  const resize = new ResizeObserver(() => {
    const width = Math.max(1, container.clientWidth), height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    draw();
  });
  resize.observe(container);
  controls.addEventListener("change", draw);
  renderer.domElement.addEventListener("webglcontextlost", event => {
    event.preventDefault();
    if (!disposed) onError(Error("3D描画が中断されました。再取得してください。"));
  });
  const blockedWorker = (event: SecurityPolicyViolationEvent) => {
    if (!disposed && event.effectiveDirective === "worker-src")
      onError(Error("この環境ではSPZの描画用Workerを起動できません。"));
  };
  document.addEventListener("securitypolicyviolation", blockedWorker);

  function disposeObject(object: THREE.Object3D) {
    const textures = new Set<THREE.Texture>();
    object.traverse(child => {
      const mesh = child as THREE.Mesh;
      mesh.geometry?.dispose();
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        if (!material) continue;
        for (const value of Object.values(material)) if (value instanceof THREE.Texture) textures.add(value);
        material.dispose();
      }
    });
    for (const texture of textures) { (texture.source.data as ImageBitmap | undefined)?.close?.(); texture.dispose(); }
  }
  return {
    reset,
    async load(bytes: ArrayBuffer, format: "glb" | "spz") {
      if (format === "glb") {
        checkGLB(bytes);
        const manager = new THREE.LoadingManager();
        manager.setURLModifier(url => {
          if (!url.startsWith("blob:") && !url.startsWith("data:"))
            throw Error("外部ファイルの読み込みは許可されていません。");
          return url;
        });
        const gltf = await new GLTFLoader(manager).parseAsync(bytes, "");
        if (disposed) { disposeObject(gltf.scene); return; }
        model = gltf.scene;
        const room = new RoomEnvironment(), pmrem = new THREE.PMREMGenerator(renderer);
        environment = pmrem.fromScene(room, 0.04);
        room.dispose(); pmrem.dispose();
        scene.environment = environment.texture;
        scene.add(model);
        bounds.setFromObject(model);
      } else {
        // Hosts may prohibit Wasm even when WebGL works. Fail before creating
        // Spark workers so the browser-view link is available immediately.
        try { new WebAssembly.Module(new Uint8Array([0,97,115,109,1,0,0,0])); }
        catch { throw Error("この環境ではSPZを表示できません。"); }
        // No network imports: esbuild embeds Spark, its worker and decoder.
        const {SparkRenderer, SplatMesh} = await import("@sparkjsdev/spark");
        if (disposed) return;
        spark = new SparkRenderer({renderer, onDirty: draw});
        scene.add(spark);
        splats = new SplatMesh({fileBytes: bytes, fileName: "model.spz", lod: false});
        // ComfyUI splats use Y-down/Z-forward; Three.js uses Y-up/Z-back.
        splats.rotation.x = Math.PI;
        scene.add(splats);
        await splats.initialized;
        if (disposed) return;
        splats.updateMatrixWorld(true);
        bounds.copy(splats.getBoundingBox(false)).applyMatrix4(splats.matrixWorld);
      }
      if (bounds.isEmpty() || !Number.isFinite(bounds.min.length() + bounds.max.length()))
        throw Error("表示できる3Dデータがありません。");
      reset();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(frame); resize.disconnect(); controls.dispose();
      document.removeEventListener("securitypolicyviolation", blockedWorker);
      splats?.dispose(); spark?.dispose();
      if (model) disposeObject(model);
      environment?.dispose(); renderer.dispose(); renderer.forceContextLoss(); renderer.domElement.remove();
    },
  };
}
