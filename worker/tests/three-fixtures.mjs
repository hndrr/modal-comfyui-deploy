import {BoxGeometry} from "three";
import {crc32, deflateSync, gzipSync} from "node:zlib";

// Small self-contained files for loader smoke checks; no model weights or downloads.
export function texturedGLB() {
  const geometry = new BoxGeometry(1.6, 1.6, 1.6), chunks = [], views = [];
  let offset = 0;
  const add = bytes => {
    const data = Buffer.from(bytes), padded = Buffer.alloc(Math.ceil(data.length / 4) * 4);
    data.copy(padded); chunks.push(padded);
    views.push({buffer:0, byteOffset:offset, byteLength:data.length}); offset += padded.length;
    return views.length - 1;
  };
  const accessors = [];
  for (const [key, type] of [["position","VEC3"],["normal","VEC3"],["uv","VEC2"]]) {
    const attribute = geometry.getAttribute(key);
    accessors.push({bufferView:add(attribute.array.buffer), componentType:5126, count:attribute.count, type,
      ...(key === "position" ? {min:[-.8,-.8,-.8], max:[.8,.8,.8]} : {})});
  }
  accessors.push({bufferView:add(geometry.index.array.buffer), componentType:5123, count:geometry.index.count, type:"SCALAR"});
  const pngChunk = (name, data) => {
    const body = Buffer.concat([Buffer.from(name), data]), result = Buffer.alloc(body.length + 8);
    result.writeUInt32BE(data.length); body.copy(result, 4); result.writeUInt32BE(crc32(body), result.length - 4);
    return result;
  };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(2); ihdr.writeUInt32BE(2, 4); ihdr[8] = 8; ihdr[9] = 6;
  const png = Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), pngChunk("IHDR",ihdr),
    pngChunk("IDAT",deflateSync(Buffer.from([0,245,150,65,255,245,230,195,255,0,245,230,195,255,35,120,150,255]))), pngChunk("IEND",Buffer.alloc(0))]);
  const textureView = add(png);
  const json = {asset:{version:"2.0"},scene:0,scenes:[{nodes:[0]}],nodes:[{mesh:0}],
    meshes:[{primitives:[{attributes:{POSITION:0,NORMAL:1,TEXCOORD_0:2},indices:3,material:0}]}],
    materials:[{pbrMetallicRoughness:{baseColorTexture:{index:0},metallicFactor:.15,roughnessFactor:.35}}],
    textures:[{source:0}],images:[{bufferView:textureView,mimeType:"image/png"}],
    accessors,bufferViews:views,buffers:[{byteLength:offset}]};
  const text = Buffer.from(JSON.stringify(json)), jsonBytes = Buffer.alloc(Math.ceil(text.length / 4) * 4, 32);
  text.copy(jsonBytes);
  const bin = Buffer.concat(chunks), header = Buffer.alloc(20), binHeader = Buffer.alloc(8);
  header.write("glTF"); header.writeUInt32LE(2,4); header.writeUInt32LE(28 + jsonBytes.length + bin.length,8);
  header.writeUInt32LE(jsonBytes.length,12); header.write("JSON",16);
  binHeader.writeUInt32LE(bin.length); binHeader.writeUInt32LE(0x004e4942,4);
  geometry.dispose();
  return Buffer.concat([header,jsonBytes,binHeader,bin]);
}

export function gaussianSPZ() {
  const count = 512, header = Buffer.alloc(16), positions = Buffer.alloc(count*9);
  const alphas = Buffer.alloc(count, 240), colors = Buffer.alloc(count*3), scales = Buffer.alloc(count*3, 121);
  const rotations = Buffer.alloc(count*3, 128);
  header.writeUInt32LE(0x5053474e); header.writeUInt32LE(2,4); header.writeUInt32LE(count,8); header[13] = 12;
  for (let i=0;i<count;i++) {
    const y = 1 - 2*(i+.5)/count, angle = i*2.3999632297, radius = Math.sqrt(1-y*y);
    for (const [axis,value] of [radius*Math.cos(angle),y,radius*Math.sin(angle)].entries()) {
      positions.writeIntLE(Math.round(value*4096),i*9+axis*3,3);
      colors[i*3+axis] = Math.round(128 + value*25);
    }
  }
  return gzipSync(Buffer.concat([header,positions,alphas,colors,scales,rotations]));
}
