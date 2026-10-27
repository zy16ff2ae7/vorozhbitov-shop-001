const fs=require('fs'),path=require('path'),sharp=require('sharp');
const root=path.resolve(__dirname,'../work'),assets=path.resolve(__dirname,'../assets'),av=fs.readFileSync(path.join(assets,'avatar.jpg')).toString('base64');
const text=(x,y,size,str,color='#fff',anchor='start',weight='700')=>`<text x="${x}" y="${y}" fill="${color}" font-family="Arial,sans-serif" font-weight="${weight}" font-size="${size}" text-anchor="${anchor}">${str}</text>`;
const avatar=(cx,cy,r)=>`<defs><clipPath id="av"><circle cx="${cx}" cy="${cy}" r="${r}"/></clipPath></defs><image href="data:image/jpeg;base64,${av}" x="${cx-r}" y="${cy-r}" width="${2*r}" height="${2*r}" clip-path="url(#av)"/>`;
const bell=(x,y,col)=>`<g transform="translate(${x},${y})" fill="none" stroke="${col}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 30h32l-4-5v-9a12 12 0 0 0-24 0v9zM19 34c1 5 9 5 10 0"/></g>`;
const thumb=(x,y,col)=>`<g transform="translate(${x},${y})" fill="none" stroke="${col}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 20h7v22H5zM12 22l9-13c1-2 5-2 6 0 2 3 0 8-1 11h12c4 0 6 3 5 7l-3 11c-1 3-3 4-6 4H12z"/></g>`;
function svg(kind,state){let sub=state>=1,b=state>=2,l=state>=3;let body='';
 if(kind==='broadcast'){
 body=`<path d="M0 38h650l-35 110H0z" fill="#141416" fill-opacity=".96"/><path d="M650 38h210l-35 110H615z" fill="#cf2d33"/><path d="M2 147h822" stroke="#ef4448" stroke-width="3"/>${avatar(64,93,48)}${text(130,84,32,'ВОРОЖБИТОВ 2.0')}${text(130,118,23,'@Vorogbitov2.0','#a9a9af')}
 <rect x="28" y="185" width="375" height="88" rx="7" fill="${sub?'#25252a':'#ce2e34'}" stroke="${sub?'#77777f':'#e3464c'}" stroke-width="2"/>${text(215,241,25,sub?'✓  ВЫ ПОДПИСАНЫ':'ПОДПИСАТЬСЯ','#fff','middle')}
 <circle cx="492" cy="229" r="46" fill="${b?'#492024':'#242428'}" stroke="${b?'#e55557':'#77777e'}" stroke-width="2"/>${bell(468,206,b?'#ff706d':'#f5f5f5')}
 <rect x="580" y="185" width="340" height="88" rx="44" fill="${l?'#492024':'#242428'}" stroke="${l?'#e55557':'#77777e'}" stroke-width="2"/>${thumb(625,203,l?'#ff706d':'#f5f5f5')}${text(770,240,25,'ЛАЙК','#fff','middle')}`;
 } else if(kind==='stack'){
 body=`<path d="M0 0H330V310H0z" fill="#a6222a"/><path d="M330 0H950V310H330z" fill="#141416" fill-opacity=".96"/><path d="M330 0v310" stroke="#ea4a4f" stroke-width="4"/>${avatar(165,91,58)}${text(165,187,24,'ВОРОЖБИТОВ 2.0','#fff','middle')}${text(165,222,20,'@Vorogbitov2.0','#e6cbcc','middle')}
 <rect x="355" y="18" width="565" height="80" rx="6" fill="${sub?'#29292e':'#cb2e34'}" stroke="#a24b4d" stroke-width="2"/>${text(638,70,27,sub?'✓  ВЫ ПОДПИСАНЫ':'ПОДПИСАТЬСЯ','#fff','middle')}
 <rect x="355" y="115" width="565" height="80" rx="6" fill="${b?'#3d2022':'#242428'}" stroke="#65656b" stroke-width="2"/>${bell(390,134,b?'#ff706d':'#fff')}${text(500,167,25,b?'УВЕДОМЛЕНИЯ ВКЛЮЧЕНЫ':'КОЛОКОЛЬЧИК')}
 <rect x="355" y="212" width="565" height="80" rx="6" fill="${l?'#3d2022':'#242428'}" stroke="#65656b" stroke-width="2"/>${thumb(390,230,l?'#ff706d':'#fff')}${text(500,263,25,'ПОСТАВЬ ЛАЙК')}`;
 } else {
 body=`<defs><linearGradient id="plate" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#2c2b30"/><stop offset=".2" stop-color="#18181c"/><stop offset="1" stop-color="#101013"/></linearGradient><linearGradient id="rim" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#88858b"/><stop offset=".5" stop-color="#45424a"/><stop offset="1" stop-color="#6e6971"/></linearGradient></defs>
 <path d="M68 119H882L900 137V282L882 300H68L50 282V137Z" fill="url(#plate)" stroke="url(#rim)" stroke-width="3"/>
 <path d="M70 126H880" stroke="#d33239" stroke-width="4"/><path d="M71 293H879" stroke="#9d272d" stroke-opacity=".75" stroke-width="2"/>
 <circle cx="475" cy="68" r="66" fill="#111113" stroke="#cd3138" stroke-width="3"/><circle cx="475" cy="68" r="61" fill="none" stroke="#b1a9ab" stroke-opacity=".65" stroke-width="2"/>${avatar(475,68,57)}
 ${text(475,165,32,'ВОРОЖБИТОВ 2.0','#fff','middle')}${text(475,198,22,'@Vorogbitov2.0','#b4b2b8','middle')}
 <rect x="68" y="226" width="475" height="68" rx="9" fill="${sub?'#29282e':'#cc3036'}" stroke="${sub?'#66636c':'#e34b50'}" stroke-width="2"/>${text(305,269,25,sub?'✓  ВЫ ПОДПИСАНЫ':'ПОДПИСАТЬСЯ','#fff','middle')}
 <rect x="578" y="226" width="72" height="68" rx="9" fill="${b?'#512023':'#2c2b30'}" stroke="${b?'#ce5558':'#66636c'}" stroke-width="2"/>${bell(590,238,b?'#ff706d':'#fff')}
 <rect x="677" y="226" width="205" height="68" rx="9" fill="${l?'#512023':'#2c2b30'}" stroke="${l?'#ce5558':'#66636c'}" stroke-width="2"/>${thumb(695,237,l?'#ff706d':'#fff')}${text(809,269,23,'ЛАЙК','#fff','middle')}`;
 }
 return `<svg xmlns="http://www.w3.org/2000/svg" width="950" height="310" viewBox="0 0 950 310">${body}</svg>`;
}
(async()=>{for(const kind of ['medal']) {fs.mkdirSync(`${root}/${kind}`,{recursive:true});for(let i=0;i<4;i++)await sharp(Buffer.from(svg(kind,i))).png().toFile(`${root}/${kind}/shorts-${i}.png`);fs.copyFileSync(path.join(assets,'cursor.png'),`${root}/${kind}/cursor.png`);fs.copyFileSync(path.join(assets,'glint.png'),`${root}/${kind}/glint.png`);}for(let i=0;i<4;i++) await sharp(`${root}/medal/shorts-${i}.png`).resize(1188,388).png().toFile(`${root}/medal/youtube-${i}.png`);console.log('medal ready')})()
