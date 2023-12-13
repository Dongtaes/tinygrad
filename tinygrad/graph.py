import os, atexit, functools
from collections import defaultdict
from typing import Dict, List
from tinygrad.ops import ScheduleItem, UnaryOps, BinaryOps, ReduceOps, MovementOps, LoadOps, BufferOps, TernaryOps, Op, OpType, LazyOp
from tinygrad.device import Device
from tinygrad.helpers import GRAPH, GRAPHPATH, DEBUG, GlobalCounters, getenv, dedup
from tinygrad.codegen.linearizer import UOps, UOp
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.symbolic import NumNode

# **** debugging and graphing ****

cnts: Dict[OpType, int] = defaultdict(int)
if DEBUG >= 2:
  def print_globalcounters():
    if GlobalCounters.time_sum_s == 0: return
    print(f"avg: {GlobalCounters.global_ops*1e-9/GlobalCounters.time_sum_s:8.2f} GFLOPS {GlobalCounters.global_mem*1e-9/GlobalCounters.time_sum_s:8.2f} GB/s",  # noqa: E501
          f"{' '*10}total: {GlobalCounters.kernel_count:5d} kernels {GlobalCounters.global_ops*1e-9:8.2f} GOPS {GlobalCounters.global_mem*1e-9:8.2f} GB {GlobalCounters.time_sum_s*1e3:8.2f} ms")  # noqa: E501
  atexit.register(print_globalcounters)
if GRAPH:
  import networkx as nx
  G = nx.DiGraph()
  def save_graph_exit():
    for k,v in cnts.items(): print(k, v)
    print("saving", G, f"to {GRAPHPATH}.svg")
    nx.drawing.nx_pydot.write_dot(G, f'{GRAPHPATH}.dot')
    # -Gnslimit=100 can make it finish, but you won't like results
    os.system(f'dot -Tsvg {GRAPHPATH}.dot -o {GRAPHPATH}.svg')
  atexit.register(save_graph_exit)

node_count = 0
def nm(x):
  global node_count
  if not hasattr(x, 'node_id'):
    setattr(x, 'node_id', node_count)
    node_count += 1
  return x.node_id

def get_sop(op: List[Op]):
  op = [x for x in op if x not in BufferOps]
  if len(op) <= 2: return '.'.join([str(y).split(".")[1] for y in op][::-1])
  if len(op) <= 6: return '.'.join([str(y).split(".")[1][0:3] for y in op][::-1])
  return str(len(op))

def str_dtype(dtyp):
  ret = str(dtyp)[7:]
  return "" if ret == 'float' else f"\n{ret}"

@functools.lru_cache(None)
def add_st_node(nmx, nmo, label, st:ShapeTracker):
  global node_count
  inter_node = node_count
  node_count += 1
  offset = st.expr_node(NumNode(0))[0]
  G.add_node(inter_node, style='filled', fillcolor="#80ff8080", color="black", label=f"{st.shape}\n{st.real_strides()}" + (f"\n{offset}" if offset != 0 else ""))  # noqa: E501
  G.add_edge(nmx, inter_node, color='#00000060')
  G.add_edge(inter_node, nmo, label=label, color='#00000060')

logops = open(getenv("LOGOPS", ""),"a") if getenv("LOGOPS", "") else None
def log_schedule_item(si: ScheduleItem):
  if logops and si.ast.op not in LoadOps: logops.write(str(si.ast)+"\n")
  if not DEBUG and not GRAPH: return
  if si.ast.op == LoadOps.CONTIGUOUS: setattr(si.out, 'node_id', nm(si.inputs[0].base))
  if si.ast.op in {LoadOps.CONST, LoadOps.CONTIGUOUS}: return

  op: List[Op] = [x.op for x in si.ast.get_lazyops()]
  oporder = [LoadOps, TernaryOps, ReduceOps, BinaryOps, UnaryOps, MovementOps, BufferOps]
  optype = type(sorted(op, key=lambda x: oporder.index(type(x)))[0])
  cnts[optype] += 1
  if GRAPH:
    assert si.out.base == si.out, "all outputs based"
    top_colors = {LoadOps: '#FFFFa0', UnaryOps: "#c0c0c0", ReduceOps: "#FFA0A0", BinaryOps: "#c0c0c0",
                  MovementOps: "#80ff80", TernaryOps: "#c0c0c0", BufferOps: '#a0a0ff'}

    # get inputs for shapetrackers
    input_to_st = defaultdict(list)
    for lo in si.ast.get_lazyops():
      if lo.op != BufferOps.LOAD: continue
      input_to_st[si.inputs[lo.arg.idx-1]].append(lo.arg.st)

    # add them to the graph, potentially with a movement op separating them
    for x in input_to_st:
      for st in dedup(input_to_st[x]):
        if st.contiguous:
          G.add_edge(nm(x), nm(si.out), label=get_sop(op), color='#00000060')
        else:
          add_st_node(nm(x), nm(si.out), get_sop(op), st)
      if 'label' not in G.nodes[nm(x)]:
        G.nodes[nm(x)]['label'] = str(x.shape)+str_dtype(si.out.dtype)

    if nm(si.out) not in G.nodes: G.add_node(nm(si.out))

    G.nodes[nm(si.out)]['label'] = '"' + (str(set(x.shape for x in si.inputs))+"\n"+str(si.out.shape) if optype == ReduceOps else str(si.out.shape))+str_dtype(si.out.dtype)+(f"\n{si.ast.op}" if si.ast.op in LoadOps or optype is BufferOps else "")+(f"\n{si.out.device}" if si.out.device != Device.DEFAULT else "") + '"'  # noqa: E501
    G.nodes[nm(si.out)]['fillcolor'] = top_colors[optype]
    G.nodes[nm(si.out)]['color'] = 'black'
    G.nodes[nm(si.out)]['style'] = 'filled'

def log_lazybuffer(lb):
  top_colors = {LoadOps: '#FFFFa0', UnaryOps: "#c0c0c0", ReduceOps: "#FFA0A0", BinaryOps: "#c0c0c0", MovementOps: "#80ff80", TernaryOps: "#c0c0c0", BufferOps: '#a0a0ff'}
  if GRAPH:
    if lb.base != lb:
      offset = lb.st.expr_node(NumNode(0))[0]
      G.add_node(nm(lb), style='filled', fillcolor="#80ff8080", color="black", label=f"{lb.st.shape}\n{lb.st.real_strides()}" + (f"\n{offset}" if offset != 0 else ""))
      G.add_edge(nm(lb.base), nm(lb), color='#00000060')
      lb = lb.base
    for x in lb.srcs:
      if nm(x) not in G.nodes: G.add_node(nm(x), label=f'"{x.realized}"', style='filled', fillcolor="#c0804080")
      G.add_edge(nm(x), nm(lb), color='#00000060')
    G.nodes[nm(lb)]['label'] = '"' + (str(set(x.shape for x in lb.srcs))+"\n"+str(lb.shape) if lb.op in ReduceOps else str(lb.shape))+str_dtype(lb.dtype)+f"\n{lb.op}"+(f"\n{lb.arg}" if lb.op == LoadOps.CONST else "")+(f"\n{lb.device}" if lb.device != Device.DEFAULT else "") + '"'
    G.nodes[nm(lb)]['fillcolor'] = [v for k,v in top_colors.items() if lb.op in k][0]
    G.nodes[nm(lb)]['color'] = 'black'
    G.nodes[nm(lb)]['style'] = 'filled'

def _tree(lazydata, prefix=""):
  if type(lazydata).__name__ == "LazyBuffer":
    return [f"━━ realized {lazydata.dtype.name} {lazydata.shape}"] if (lazydata.realized) else _tree(lazydata.op, "LB ")
  if len(lazydata.src) == 0: return [f"━━ {prefix}{lazydata.op.name} {lazydata.arg if lazydata.arg else ''}"]
  lines = [f"━┳ {prefix}{lazydata.op.name} {lazydata.arg if lazydata.arg else ''}"]
  childs = [_tree(c) for c in lazydata.src[:]]
  for c in childs[:-1]: lines += [f" ┣{c[0]}"] + [f" ┃{l}" for l in c[1:]]
  return lines + [" ┗"+childs[-1][0]] + ["  "+l for l in childs[-1][1:]]

def print_tree(lazydata:LazyOp): print("\n".join([f"{str(i).rjust(3)} {s}" for i,s in enumerate(_tree(lazydata))]))

def graph_uops(uops:List[UOp]):
  import networkx as nx
  colors = {UOps.ALU: "#ffffc0", UOps.LOAD: "#ffc0c0", UOps.STORE: "#c0ffc0", UOps.SPECIAL: "#c0c0ff", UOps.CONST: "#e0e0e0",
            UOps.DEFINE_GLOBAL: "#ffe0b0", UOps.DEFINE_LOCAL: "#ffe0d0", UOps.DEFINE_ACC: "#f0ffe0",
            UOps.LOOP: "#c8a0e0", UOps.PHI: "#e0ffc0", UOps.BARRIER: "#ff8080", UOps.IF: "#c8b0c0"}
  G = nx.DiGraph()
  for u in uops:
    if u.uop == UOps.END: continue
    G.add_node(uops.index(u), label=f"{str(u.uop)[5:]}{(' '+str(u.arg)) if u.arg is not None else ''}\n{str(u.dtype)}", style="filled", fillcolor=colors.get(u.uop, "#ffffff"))  # noqa: E501
    for v in u.vin: G.add_edge(uops.index(v), uops.index(u))
  GRAPHPATH = "/tmp/uops"
  nx.drawing.nx_pydot.write_dot(G, f'{GRAPHPATH}.dot')
  os.system(f'dot -Grankdir=LR -Tsvg {GRAPHPATH}.dot -o {GRAPHPATH}.svg')
