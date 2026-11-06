// Runs inside the player before decodeAudioData allocates an entire PCM buffer.
// Packet/frame counts bound expansion; container duration tags alone are not trusted.
export const audioBudgetScript = String.raw`
function audioBudget(b,mime){
  const view=new DataView(b.buffer,b.byteOffset,b.byteLength), length=b.length;
  const tag=(at,s)=>at+s.length<=length && [...s].every((c,i)=>b[at+i]===c.charCodeAt(0));
  const u16=at=>view.getUint16(at,true), u32=at=>view.getUint32(at,true);
  const result=(seconds,channels)=>({seconds,pcmBytes:Math.ceil(seconds*48000)*channels*4});
  try {
    if(mime==='audio/wav'){
      if(!tag(0,'RIFF') || !tag(8,'WAVE') || u32(4)+8!==length)return;
      let channels,rate,align,bytes=0,hasFormat=false,hasData=false;
      for(let at=12;at+8<=length;){
        const size=u32(at+4), end=at+8+size;
        if(end>length)return;
        if(tag(at,'fmt ')){
          if(hasFormat || size<16 || ![1,3,65534].includes(u16(at+8)))return;
          hasFormat=true;
          if(u16(at+8)===65534 && (size<40 || ![1,3].includes(u32(at+32)) ||
             ![0,0,16,0,128,0,0,170,0,56,155,113].every((v,i)=>b[at+36+i]===v)))return;
          channels=u16(at+10);rate=u32(at+12);align=u16(at+20);
          const bits=u16(at+22);
          if(!bits || bits%8 || align!==channels*bits/8)return;
        }else if(tag(at,'data')){if(hasData)return;hasData=true;bytes+=size;}
        at=end+(size%2);
      }
      if(!bytes || !align || bytes%align || !rate || !channels || channels>32)return;
      return result(bytes/align/rate,channels);
    }
    if(mime==='audio/flac'){
      if(!tag(0,'fLaC') || length<42 || (b[4]&127)!==0 || (b[5]*65536+b[6]*256+b[7])!==34)return;
      const rate=b[18]*4096+b[19]*16+(b[20]>>4), channels=((b[20]>>1)&7)+1;
      if(!rate)return;
      const declared=(b[21]&15)*4294967296+view.getUint32(22,false);
      let at=4;
      for(;;){
        if(at+4>length)return;
        if(at!==4 && (b[at]&127)===0)return;
        const last=b[at]&128,size=b[at+1]*65536+b[at+2]*256+b[at+3];
        at+=4+size;if(at>length)return;if(last)break;
      }
      let seconds=0, found=0, maxChannels=channels;
      const blocks=[0,192,576,1152,2304,4608,0,0,256,512,1024,2048,4096,8192,16384,32768];
      const rates=[rate,88200,176400,192000,8000,16000,22050,24000,32000,44100,48000,96000];
      for(let i=at;i+6<length;i++){
        if(b[i]!==255 || (b[i+1]&254)!==248 || b[i+3]&1 || b[i+3]>>4>10)continue;
        let k=i+4, first=b[k++], n=first<128?0:first>=192 && first<224?1:first<240?2:first<248?3:first<252?4:first<254?5:first===254?6:-1;
        if(first>=128 && first<192 || n<0)continue;
        while(n>0 && k<length && (b[k]&192)===128){k++;n--;}
        if(n || k+5>length)continue;
        const code=b[i+2]>>4, rc=b[i+2]&15;
        let frames=blocks[code], hz=rates[rc];
        if(code===6)frames=b[k++]+1;
        if(code===7){frames=b[k]*256+b[k+1]+1;k+=2;}
        if(rc===12)hz=b[k++]*1000;
        if(rc===13 || rc===14){hz=(b[k]*256+b[k+1])*(rc===14?10:1);k+=2;}
        if(!frames || !hz || k>=length)continue;
        let crc=0;
        for(let j=i;j<k;j++){crc^=b[j];for(let bit=0;bit<8;bit++)crc=((crc<<1)^((crc&128)?7:0))&255;}
        if(crc!==b[k])continue;
        // Count every plausible frame header. False positives can only make
        // the bound more conservative, never hide long constant-value frames.
        seconds+=frames/Math.min(rate,hz);found++;
        maxChannels=Math.max(maxChannels,(b[i+3]>>4)<8?(b[i+3]>>4)+1:2);
        if(seconds>600)return result(seconds,maxChannels);
      }
      if(!found)return;
      return result(Math.max(seconds,declared/rate),maxChannels);
    }
    if(mime==='audio/mpeg'){
      let at=0,seconds=0,channels=1,frames=0;
      if(tag(0,'ID3')){
        if(length<10 || [b[6],b[7],b[8],b[9]].some(v=>v&128))return;
        at=10+b[6]*2097152+b[7]*16384+b[8]*128+b[9]+((b[5]&16)?10:0);
      }
      while(at+4<=length){
        if(length-at===128 && tag(at,'TAG')){at=length;break;}
        if(b[at]!==255 || (b[at+1]&224)!==224)return;
        const version=(b[at+1]>>3)&3, layer=(b[at+1]>>1)&3;
        const bitrateIndex=b[at+2]>>4, sampleIndex=(b[at+2]>>2)&3;
        if(version===1 || layer!==1 || !bitrateIndex || bitrateIndex===15 || sampleIndex===3)return;
        const rate=[44100,48000,32000][sampleIndex]/(version===3?1:version===2?2:4);
        const kbps=(version===3?[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320]:[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160])[bitrateIndex];
        const size=Math.floor((version===3?144000:72000)*kbps/rate)+((b[at+2]>>1)&1);
        if(size<4 || at+size>length)return;
        seconds+=(version===3?1152:576)/rate;channels=Math.max(channels,(b[at+3]>>6)===3?1:2);frames++;
        if(seconds>600)return result(seconds,channels);
        at+=size;
      }
      if(at!==length || !frames)return;
      return result(seconds,channels);
    }
    if(mime.startsWith('audio/ogg')){
      let at=0,serial,sequence,packets=0,packetSize=0,head=[],codec,rate,channels,maxBlock,seconds=0,eos=false;
      while(at<length){
        if(eos || !tag(at,'OggS') || b[at+4]!==0 || at+27>length)return;
        const count=b[at+26],id=u32(at+14),seq=u32(at+18),flags=b[at+5];
        if(at+27+count>length || (serial!==undefined && (id!==serial || seq!==sequence+1)) ||
           ((flags&1)!==0)!==(packetSize>0))return;
        serial=id;sequence=seq;
        let cursor=at+27+count;
        for(let i=0;i<count;i++){
          const size=b[at+27+i];if(cursor+size>length)return;
          for(let j=0;j<size && head.length<32;j++)head.push(b[cursor+j]);
          packetSize+=size;cursor+=size;
          if(size===255)continue;
          const match=s=>[...s].every((c,j)=>head[j]===c.charCodeAt(0));
          if(packets===0){
            if(match('OpusHead') && packetSize>=19){codec='opus';rate=48000;channels=head[9];}
            else if(head[0]===1 && head.slice(1,7).map(v=>String.fromCharCode(v)).join('')==='vorbis' && packetSize>=30){
              codec='vorbis';channels=head[11];rate=head[12]+head[13]*256+head[14]*65536+head[15]*16777216;
              maxBlock=2**(head[28]>>4);if(maxBlock>8192)return;
            }else return;
            if(!rate || !channels || channels>32)return;
          }else if(codec==='opus' && packets===1){if(!match('OpusTags'))return;}
          else if(codec==='vorbis' && packets<3){
            if(head[0]!==[0,3,5][packets] || head.slice(1,7).map(v=>String.fromCharCode(v)).join('')!=='vorbis')return;
          }else if(codec==='opus'){
            if(!packetSize)return;
            const config=head[0]>>3, code=head[0]&3;
            const frames=code===0?1:code<3?2:(head[1]&63);
            const ms=config<12?[10,20,40,60][config%4]:config<16?[10,20][config%2]:[2.5,5,10,20][config%4];
            if(!frames || (code===3 && packetSize<2) || ms*frames>120)return;
            seconds+=ms*frames/1000;
          }else{if(!packetSize || head[0]&1)return;seconds+=maxBlock/2/rate;}
          packets++;packetSize=0;head=[];
          if(seconds>600)return result(seconds,channels);
        }
        eos=!!(flags&4);at=cursor;
      }
      if(!eos || packetSize || !seconds)return;
      return result(seconds,channels);
    }
  }catch{}
}
`;
