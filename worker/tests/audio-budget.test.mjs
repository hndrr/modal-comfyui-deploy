import assert from 'node:assert/strict';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';
import {audioBudgetScript} from '../src/audio-budget.ts';
import {shortFlac} from './audio-fixtures.mjs';
const budget=runInNewContext(audioBudgetScript+';audioBudget');
const permitted=b=>!!b && b.seconds<=600 && b.pcmBytes<=128*1024*1024;

test('valid small FLAC is playable; an understated duration cannot hide many frames',()=>{
  const small=budget(shortFlac,'audio/flac');
  assert.equal(small.seconds,0.1);assert.equal(small.pcmBytes,19200);
  const frame=shortFlac.slice(42), long=new Uint8Array(42+frame.length*7000);
  long.set(shortFlac.slice(0,42));
  for(let n=0;n<7000;n++)long.set(frame,42+n*frame.length);
  // STREAMINFO still claims 0.1s, while frames expand past ten minutes.
  assert.ok(long.length<4*1024*1024);
  assert.equal(permitted(budget(long,'audio/flac')),false);
  assert.equal(budget(new Uint8Array([102,76,97,67]),'audio/flac'),undefined);
});

test('low bitrate MP3 is checked by frame count, not just compressed bytes',()=>{
  // MPEG-2.5 Layer III, 8 kbit/s, 8 kHz mono: 72-byte frame / 576 samples.
  const frame=new Uint8Array(72);frame.set([255,227,24,192]);
  assert.equal(budget(frame,'audio/mpeg').seconds,0.072);
  const long=new Uint8Array(frame.length*9000);
  for(let n=0;n<9000;n++)long.set(frame,n*frame.length);
  assert.ok(long.length<1024*1024);
  assert.equal(permitted(budget(long,'audio/mpeg')),false);
  assert.equal(budget(frame.slice(0,20),'audio/mpeg'),undefined);
});

test('WAV bounds use frame alignment and the fixed resampling rate',()=>{
  const wav=Buffer.alloc(44+16000);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);
  wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);
  wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(16000,40);
  assert.equal(budget(wav,'audio/wav').pcmBytes,192000);
  wav.writeUInt16LE(0,32);assert.equal(budget(wav,'audio/wav'),undefined);
  assert.equal(budget(wav,'audio/unknown'),undefined);
});

function page(packets,sequence,flags=0){
  const size=27+packets.length+packets.reduce((n,p)=>n+p.length,0),b=Buffer.alloc(size);
  b.write('OggS');b[5]=flags;b.writeUInt32LE(1,14);b.writeUInt32LE(sequence,18);b[26]=packets.length;
  let at=27+packets.length;
  packets.forEach((p,i)=>{b[27+i]=p.length;b.set(p,at);at+=p.length;});return b;
}
test('Opus duration derives from packets even if Ogg granule timestamps understate it',()=>{
  const head=Buffer.alloc(19);head.write('OpusHead');head[8]=1;head[9]=2;
  const start=page([head],0,2),tags=page([Buffer.from('OpusTags')],1);
  const packet=Buffer.from([24]); // SILK 60 ms, one frame.
  const short=Buffer.concat([start,tags,page([packet],2,4)]);
  assert.equal(budget(short,'audio/ogg; codecs=opus').seconds,0.06);
  const pages=[start,tags];
  for(let n=0;n<50;n++)pages.push(page(Array(250).fill(packet),n+2,n===49?4:0));
  const long=Buffer.concat(pages);
  assert.ok(long.length<32768);
  assert.equal(permitted(budget(long,'audio/ogg; codecs=opus')),false);
  assert.equal(budget(short.subarray(0,short.length-1),'audio/ogg'),undefined);
});
