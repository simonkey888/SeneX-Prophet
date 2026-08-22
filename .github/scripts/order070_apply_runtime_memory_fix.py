from pathlib import Path
import re

p=Path('senecio_polymarket/backend/main.py')
s=p.read_text()
old_research='''# ACT-XXVII: research layer (lazy-initialized to avoid import-time failures
# if optional deps like shap are missing)
try:
    from .research import ResearchCoordinator, get_registry
    _research_coord = ResearchCoordinator()
    _metrics_registry = get_registry()
except Exception as _research_init_err:  # pragma: no cover — research layer must never break the app
    _research_coord = None
    _metrics_registry = None
    _research_init_err_msg = str(_research_init_err)
else:
    _research_init_err_msg = None
'''
new_research='''# ACT-XXVII research is optional for the production oracle.  Keep heavy
# numpy/scipy/sklearn/shap modules out of the 512 MiB runtime until an endpoint
# explicitly asks for research functionality.
_research_coord = None
_metrics_registry = None
_research_init_err_msg = None
_research_init_attempted = False


def _ensure_research() -> None:
    global _research_coord, _metrics_registry, _research_init_err_msg, _research_init_attempted
    if _research_init_attempted:
        return
    _research_init_attempted = True
    try:
        from .research import ResearchCoordinator, get_registry
        _research_coord = ResearchCoordinator()
        _metrics_registry = get_registry()
        _research_init_err_msg = None
    except Exception as exc:  # pragma: no cover — optional layer must never break app
        _research_coord = None
        _metrics_registry = None
        _research_init_err_msg = str(exc)
'''
old_af='''# ACT-XXIX: anti-fragility layer (lazy-initialized; never breaks the app)
try:
    from .antifragility import AntiFragilityCoordinator as _AFCoord
    _antifragility_coord = _AFCoord(start_biv=False)
except Exception as _af_init_err:  # pragma: no cover
    _antifragility_coord = None
    _af_init_err_msg = str(_af_init_err)
else:
    _af_init_err_msg = None
'''
new_af='''# ACT-XXIX anti-fragility is also optional on the public production path.
_antifragility_coord = None
_af_init_err_msg = None
_af_init_attempted = False


def _ensure_antifragility() -> None:
    global _antifragility_coord, _af_init_err_msg, _af_init_attempted
    if _af_init_attempted:
        return
    _af_init_attempted = True
    try:
        from .antifragility import AntiFragilityCoordinator as _AFCoord
        _antifragility_coord = _AFCoord(start_biv=False)
        _af_init_err_msg = None
    except Exception as exc:  # pragma: no cover
        _antifragility_coord = None
        _af_init_err_msg = str(exc)
'''
assert old_research in s, 'research eager-init block drifted'
assert old_af in s, 'antifragility eager-init block drifted'
s=s.replace(old_research,new_research,1).replace(old_af,new_af,1)

# Existing endpoint guards are the correct lazy boundary.  Ensure the optional
# subsystem immediately before each guard, preserving all endpoint semantics.
lines=s.splitlines(True)
out=[]
for line in lines:
    stripped=line.lstrip()
    indent=line[:len(line)-len(stripped)]
    helper=None
    if stripped.startswith('if _research_coord') or stripped.startswith('if _metrics_registry'):
        helper='_ensure_research()'
    elif stripped.startswith('if _antifragility_coord'):
        helper='_ensure_antifragility()'
    if helper and (not out or out[-1].strip()!=helper):
        out.append(indent+helper+'\n')
    out.append(line)
s=''.join(out)

# Defensive contract: production import must remain lazy, while the legacy
# endpoints still have on-demand initializers available.
assert '_research_init_attempted = False' in s
assert '_af_init_attempted = False' in s
assert s.count('_ensure_research()') >= 8
assert s.count('_ensure_antifragility()') >= 5
p.write_text(s)
print('PATCH=OPTIONAL_ANALYTICS_TRUE_LAZY_INIT')
print('RESEARCH_GUARDS='+str(s.count('_ensure_research()')))
print('ANTIFRAGILITY_GUARDS='+str(s.count('_ensure_antifragility()')))
