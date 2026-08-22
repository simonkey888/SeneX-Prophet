from pathlib import Path
import ast

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
new_research='''# ACT-XXVII research is optional for the production oracle. Keep heavy
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

# Parse the still-valid source, then place one initializer at a function
# boundary rather than before arbitrary multiline expressions. This preserves
# syntax and avoids initializing optional analytics on the production import path.
tree=ast.parse(s)
lines=s.splitlines(True)
insertions=[]
research_functions=[]
antifragility_functions=[]
for node in tree.body:
    if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
        continue
    if node.name in {'_ensure_research','_ensure_antifragility'}:
        continue
    names={n.id for n in ast.walk(node) if isinstance(n,ast.Name)}
    helpers=[]
    if names & {'_research_coord','_metrics_registry'}:
        helpers.append('_ensure_research()')
        research_functions.append(node.name)
    if '_antifragility_coord' in names:
        helpers.append('_ensure_antifragility()')
        antifragility_functions.append(node.name)
    if not helpers:
        continue
    first=node.body[0]
    if (isinstance(first,ast.Expr) and isinstance(first.value,ast.Constant)
            and isinstance(first.value.value,str)):
        insertion_index=first.end_lineno
    else:
        insertion_index=first.lineno-1
    indent=' ' * int(getattr(first,'col_offset',4) or 4)
    insertions.append((insertion_index,''.join(indent+h+'\n' for h in helpers)))

for index,text in sorted(insertions,reverse=True):
    lines[index:index]=[text]
s=''.join(lines)

# Syntax is a hard pre-write gate. Also prove the production module now has
# explicit on-demand boundaries for both optional heavy subsystems.
ast.parse(s)
assert '_research_init_attempted = False' in s
assert '_af_init_attempted = False' in s
assert len(research_functions) >= 8, research_functions
assert len(antifragility_functions) >= 5, antifragility_functions
p.write_text(s)
print('PATCH=OPTIONAL_ANALYTICS_TRUE_LAZY_INIT_R2')
print('RESEARCH_FUNCTIONS='+','.join(research_functions))
print('ANTIFRAGILITY_FUNCTIONS='+','.join(antifragility_functions))
print('FINAL_AST_PARSE=PASS')
