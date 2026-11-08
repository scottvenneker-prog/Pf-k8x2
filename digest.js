// Maandrapport: draait de echte app headless, leest cijfer/risico's/sterke punten/koopadvies en stuurt één ntfy-push. Alleen percentages en namen, geen bedragen.
const {chromium}=require('playwright'),fs=require('fs');
const BASE=process.env.BASE||'http://localhost:8080',PW=process.env.PF_PW,TOPIC=process.env.NTFY_TOPIC;
if(!PW){console.log('Geen PF_PW, maandrapport overgeslagen');process.exit(0)}
(async()=>{const b=await chromium.launch(),p=await b.newPage({viewport:{width:400,height:900}});const err=[];p.on('pageerror',x=>err.push(x.message));
await p.addInitScript(pw=>{localStorage.setItem('pf_pw',pw)},PW);await p.goto(BASE+'/index.html');await p.waitForSelector('.nav button',{timeout:30000});await p.waitForTimeout(2500);
const o=await p.evaluate(()=>{const R=rapport(),K=kandPick(typeof INLEG==='number'?INLEG:2000),cl=s=>String(s).replace(/<[^>]+>/g,'');
 const pc=x=>/^[0-9][0-9.,]*%$/.test(String(x))?String(x):'';
 return {c:R.tot,gr:R.gr,risk:risicos().slice(0,3).map(x=>({k:x.k,t:cl(x.t),n:pc(x.n)})),sterk:sterkten().slice(0,3).map(x=>({k:x.k,t:cl(x.t),n:pc(x.n)})),pick:K.top.map(x=>x.r.naam),over:own.filter(r=>verdict(r).k==='structureel').map(r=>r.naam)}});
await b.close();if(err.length)console.log('Fouten in de app:',err.slice(0,3));
let prev={};try{prev=JSON.parse(fs.readFileSync('maand.json','utf8'))}catch(e){}
const f1=x=>x.toFixed(1).replace('.',','),d=prev.c!=null?o.c-prev.c:null,pk=new Set(prev.risk||[]),ps=new Set(prev.sterk||[]);
const L=[`Cijfer ${f1(o.c)} (${o.gr})`+(d==null?'':Math.abs(d)<0.05?', gelijk aan vorige maand':`, ${d>0?'+':'−'}${f1(Math.abs(d))} sinds vorige maand`),
 `Grootste risico's: `+o.risk.map((x,i)=>`${i+1}. ${x.t}${x.n?' ('+x.n+')':''}${prev.risk&&!pk.has(x.k)?' NIEUW':''}`).join(' · '),
 `Sterkste punten: `+o.sterk.map(x=>`${x.t}${x.n?' ('+x.n+')':''}${prev.sterk&&!ps.has(x.k)?' NIEUW':''}`).join(' · '),
 o.over.length?`Verkoop overwegen: ${o.over.join(', ')}`:`Niets op verkoop overwegen`,
 `Deze maand bijkopen: ${o.pick.join(', ')||'geen duidelijke kandidaat'}`];
const msg=L.join('\n');console.log(msg);
fs.writeFileSync('maand.json',JSON.stringify({datum:new Date().toISOString().slice(0,10),c:o.c,risk:o.risk.map(x=>x.k),sterk:o.sterk.map(x=>x.k),pick:o.pick}));
if(TOPIC&&!process.env.DRY){const r=await fetch('https://ntfy.sh/'+TOPIC,{method:'POST',headers:{Title:('Maandrapport portefeuille'),Click:'https://scottvenneker-prog.github.io/Pf-k8x2/'},body:msg});console.log('ntfy',r.status)}
})().catch(e=>{console.error(e);process.exit(1)});
