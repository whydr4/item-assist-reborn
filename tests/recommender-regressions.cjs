const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../mod_src/panorama/scripts/ql_feat_recommender.js'), 'utf8');
function context(names, values) {
    const ctx = vm.createContext(values);
    for (const name of names) {
        const start = source.indexOf('    function ' + name + '(');
        assert.ok(start >= 0, name);
        vm.runInContext(source.slice(start, source.indexOf('\n    }', start) + 6), ctx);
    }
    return ctx;
}
const ordering = context(['orderPurchases'], {});
const items = () => [{id:'cheap',tier:1,cost:800},{id:'goal',tier:4,cost:6400},{id:'mid',tier:3,cost:3200}];
let picks = ordering.orderPurchases(items(), 900, 2400, 8);
assert.equal(picks[0].id, 'goal');
assert.equal(picks[0].aff, false);
assert.equal(picks.find(x => x.id === 'cheap').aff, true);
picks = ordering.orderPurchases(items(), 0, 2400, 8);
assert.equal(picks[0].id, 'goal');
assert.ok(picks.every(x => !x.aff));
assert.equal(ordering.orderPurchases(items(), 7000, 2400, 8)[0].aff, true);
const inv = context(['purchaseAdvice','rememberAdvice','markOwned','logOwned'], {
    DATA:{items:{'1':{n:'Base'},'2':{n:'Upgrade',comp:['1']}}},
    _ownedAccum:{},_ownedAt:{},_ownedMissing:{},_soldAwaitClear:{},_inventoryChanges:{},
    coveredComponents:owned=>owned.upgrade?{'1':1}:{},
    _adviceHistory:[],_lastCand:[{id:'1'},{id:'2'}],_hb:10,_ownLog:[],_logOwned:{},_heroId:null,
    nameToId:n=>({base:'1',upgrade:'2'})[n],itemName:id=>id,
    markCovered:(id,out)=>{if(String(id)==='2')out['1']=1;},ilog:()=>{}
});
inv.rememberAdvice([{id:'1'},{id:'2'}],{});
inv.markOwned('1',true,'toast');
inv.rememberAdvice([{id:'2'}],inv._ownedAccum); // UI already refreshed after buy
let events=[];inv.ilog=(ev,o)=>events.push({ev,...o});inv.logOwned();
assert.equal(events[0].rank,0);
assert.equal(events[0].ev,'buy');
inv.markOwned('2',true,'toast');inv.logOwned();
assert.ok(!inv._ownedAccum.base);
assert.ok(events.some(e=>e.ev==='component_consumed' && e.parent===2));
inv.markOwned('2',false,'sold');inv.logOwned();
assert.equal(inv._soldAwaitClear.upgrade,1);
assert.equal(events.at(-1).ev,'sell');
inv.markOwned('1',true,'shop');inv.logOwned();
assert.equal(events.at(-1).ev,'inventory_sync');
// Enemy labels may arrive in different classes; both must be merged, with duplicates removed.
const heroes = context(['heroesInPanel'], {_heroLabel:'',resolveHeroToken:t=>({one:1,two:2})[t]});
assert.deepEqual(Array.from(heroes.heroesInPanel({FindChildrenWithClassTraverse:c=>c==='PlayerHeroHidden'?[{text:'one'}]:[{text:'one'},{text:'two'}]})),[1,2]);
const rank = context(['detectRank'], {_rankTier:null,_rankTries:30,_rankNextAt:0,_hb:100,hasCls:()=>false});
rank.detectRank({FindChildTraverse:()=>null});assert.equal(rank._rankTries,31);
console.log('PASS: saving goal and zero balance; pre-purchase advice; upgrade consumption; sales; inventory evidence; partial enemy labels; rank retries');
const cards = context(['getOwnedNames'], {
    _ownedAccum:{base:1},_ownedAt:{base:0},_ownedMissing:{},_soldAwaitClear:{},_staleCards:[],_ownedDiagN:0,_hb:10,
    _vis:e=>e.visible,cardOwned:e=>e.own,hasCls:()=>false,canonItem:t=>t.toLowerCase(),nameToId:()=> '1',
    coveredComponents:()=>({}),markOwned:(id,on)=>{if(!on)delete cards._ownedAccum.base;}
});
const card={visible:true,actuallayoutwidth:100,own:false,FindChildrenWithClassTraverse:()=>[{text:'Base'}]};
const shop={FindChildrenWithClassTraverse:()=>[card]};
cards.getOwnedNames(shop);cards.getOwnedNames(shop);assert.equal(cards._ownedAccum.base,1);
cards._hb=11;cards.getOwnedNames(shop);assert.equal(cards._ownedAccum.base,1);
cards._hb=13;cards.getOwnedNames(shop);assert.ok(!cards._ownedAccum.base);
cards._ownedAccum.base=1;card.visible=false;cards._hb=30;cards.getOwnedNames(shop);assert.equal(cards._ownedAccum.base,1);
let additions=0;cards.markOwned=()=>additions++;cards._ownedAccum={};cards._soldAwaitClear.base=1;card.visible=true;card.own=true;
cards.getOwnedNames(shop);assert.equal(additions,0);
card.own=false;cards.getOwnedNames(shop);card.own=true;cards.getOwnedNames(shop);assert.equal(additions,1);
console.log('PASS: distinct-tick inventory reconciliation, hidden cards ignored, sold-card stale state blocked until cleared');
const queueCalls = [];
const dispatch = context(['quickbuyCommand','qbAddItem'], {$:{DispatchEvent:(...args)=>queueCalls.push(args)},ilog:()=>{}});
assert.equal(dispatch.qbAddItem('3776945997'),true);
assert.deepEqual(queueCalls[0], ['CitadelConCommand','quickbuy add 3776945997 0 false 0']);
assert.equal(dispatch.qbAddItem('1; quit'),false);
assert.equal(dispatch.qbAddItem('-1'),false);
assert.equal(dispatch.qbAddItem('4294967296'),false);
assert.equal(queueCalls.length,1);
console.log('PASS: native quick-buy command transport, unsigned token preserved, malformed IDs rejected');
const dataContext=vm.createContext({});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../data/recommender_data.js'),'utf8'),dataContext);
const data=dataContext.QOL_REC_DATA;
const localization=context(['norm','buildNameIndex','nameToId'],{DATA:data,_nameToId:null,_nameLang:null,_uiLang:null});
for(const lang of ['english','russian']) {
    for(const id of ['1437614329','465043967','3776945997','26002154','1342610602','754480263']) {
        const label=data.loc[lang].i[id];
        assert.ok(label,`${lang}: ${id}`);
        assert.equal(localization.nameToId(label),id,`${lang}: ${label}`);
    }
}
console.log('PASS: real English and Russian quick-buy labels resolve to the same six item IDs');

const tasks=[],actual=[];
const sync=context(['queueSync'],{
    CFG:{qsync:true},_heroId:35,_hb:10,_qsNextAt:0,_qsGeneration:0,QS_N:3,_enemies:[],
    queueSetup:()=>{},getSouls:()=>600,getOwnedNames:()=>({}),getGameTimeSec:()=>10,
    computeRecs:()=>[{id:'1'},{id:'2'},{id:'3'}],qbQueue:()=>actual.map(id=>({id})),
    quickbuyCommand:()=>{actual.length=0;return true;},qbAddItem:id=>actual.push(id),
    ilog:()=>{},_qsSetupLog:'ready',valid:()=>true,_qbPanel:{},
    $:{Schedule:(delay,fn)=>tasks.push({delay,fn})}
});
sync.queueSync({},{});
// Execute the add callbacks in time order, separately from the delayed result callback.
while(tasks.some(t=>t.delay<4)) {const index=tasks.findIndex(t=>t.delay<4);tasks.splice(index,1)[0].fn();}
assert.deepEqual(actual,['1','2','3']);
sync._hb=20;sync.queueSync({},{});assert.deepEqual(actual,['1','2','3']);
console.log('PASS: append transport preserves recommendation order and stable queue is not rebuilt');
