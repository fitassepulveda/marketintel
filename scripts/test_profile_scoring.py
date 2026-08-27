"""Offline test of per-profile semantic scoring (stubbed LLM — Gemini unreachable)."""
import json, sqlite3, sys, shutil, os
sys.path.insert(0, '.')
from src import profiles as P
from src.prioritize import profile_relevance as PR
import yaml

DB = os.path.expanduser('~/scratch/test_profile.db')
shutil.copy(os.path.expanduser('~/scratch/intel.db'), DB)
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
PR.ensure_table(con)

raw = yaml.safe_load(open('config/profiles.yaml')); d = raw['defaults']
rafic = {**d, **next(p for p in raw['profiles'] if p['name'] == 'Rafic')}
rafic['_weights'] = P.dimension_weights(rafic)

pool = [dict(r) for r in con.execute(
    "select id,title,summary,source,area,composite_score from articles "
    "where substr(fetched,1,10)='2026-08-25' and llm_score is not null "
    "order by composite_score desc limit 18")]
for a in pool:
    a['summary'] = a.get('summary') or ''

# --- stub: emulate what the role-scored model would return -------------------
SCORES = {4001:(8.9,'Distressed commercial real estate in the outpatient expansion footprint'),
          3998:(6.4,'Competitor leadership change at nearby hospitals'),
          3999:(6.8,'Competitor leadership change in an adjacent county'),
          4014:(2.1,'Drug approval — pharmaceutical, outside the outpatient remit'),
          4008:(2.4,'EHR vendor and AI partnership — IT programme, another leader'),
          4009:(2.6,'Hospital IT/AI control story, not outpatient capacity'),
          3985:(2.2,'IT security notice, no outpatient access impact'),
          4006:(6.1,'Cost-control penalties could shift site-of-care economics'),
          4061:(0.9,'Unrelated to healthcare operations'),
          3986:(1.8,'Personnel at a distant system'),
          3987:(3.4,'Labour action, no regional or outpatient link'),
          3989:(1.6,'Distant facility safety item'),
          3990:(1.9,'Legal dispute at a distant system'),
          3991:(2.8,'Service-line staffing gap, distant'),
          3992:(1.4,'Federal tracking item, no outpatient link'),
          3995:(2.0,'General leadership commentary'),
          4049:(1.1,'Global outbreak, no regional link'),
          4060:(1.1,'Global outbreak, no regional link')}
BY_TITLE = {}
class StubClient:
    """Resolves each item by the TITLE in the prompt, so batch offsets are exercised
    for real rather than assumed (the first version of this stub keyed on the local
    batch index and silently mis-assigned every item in batch 2)."""
    def complete(self, model, system, prompt, max_tokens=4000):
        assert 'outpatient' in system.lower() or 'outpatient' in prompt.lower()
        out = []
        titles = [l.split('title: ',1)[1].strip()
                  for l in prompt.split('\n') if l.strip().startswith('title: ')]
        for i, t in enumerate(titles):
            aid = BY_TITLE[t]
            sc, why = SCORES.get(aid, (3.0, 'default'))
            out.append({"i": i, "score": sc, "why": why})
        return json.dumps(out)

BY_TITLE.update({a['title']: a['id'] for a in pool})
res = PR.score_batch(StubClient(), 'stub', {'name':'UM','description':'x','region':'SF'}, rafic, pool)
for a, (sc, why) in zip(pool, res):
    PR.save(con, a['id'], 'Rafic', sc, why); a['profile_score'] = sc
con.commit()

print('=== caching: second call should score 0 new items ===')
cached = PR.load_all(con, 'Rafic')
print(f'  cached scores: {len(cached)}   todo on rerun: {len([a for a in pool if a["id"] not in cached])}')

print('\n=== Rafic (semantic, role-scored) vs the shared briefing — 2026-08-25 ===')
for a in pool: a['rafic'] = P.personal_composite(rafic, a)
house = sorted(pool, key=lambda x: -(x['composite_score'] or 0))[:6]
raf = sorted(pool, key=lambda x: -x['rafic'])[:6]
print(f"{'SHARED BRIEFING':<52}| RAFIC")
for h, r in zip(house, raf):
    print(f" [{h['composite_score']:5.1f}] {h['title'][:42]:<44}| [{r['rafic']:5.1f}] {r['title'][:42]}")

print(f"\n=== scale check (threshold {rafic['threshold']}) ===")
passing = [a for a in pool if a['rafic'] >= float(rafic['threshold'])]
print(f'  house scores  : {min(a["composite_score"] for a in pool):.0f}-{max(a["composite_score"] for a in pool):.0f}')
print(f'  Rafic min/max : {min(a["rafic"] for a in pool):.1f} / {max(a["rafic"] for a in pool):.1f}')
print(f'  passing 55    : {len(passing)} of {len(pool)}  -> {[a["title"][:34] for a in passing]}')
