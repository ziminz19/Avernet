# 校验 undercover.py 渲染出来的一次性自定义协作 YAML 是否满足 BCS 运行时契约。
# 用法: ruby -ryaml undercover_run_check.rb <yaml 文件> <speak|vote>
path, kind = ARGV[0], ARGV[1]
abort "usage: undercover_run_check.rb <yaml> <speak|vote>" unless path && %w[speak vote].include?(kind)

d = YAML.safe_load(File.read(path, encoding: "UTF-8"))
abort "unexpected top-level keys: #{d.keys}" unless (d.keys - %w[name metadata participants runtime]).empty?
abort "name must be non-empty" if d["name"].to_s.empty?
abort "participants must be non-empty" if d["participants"].to_s.empty?
abort "runtime.kind must be state_machine" unless d.dig("runtime", "kind") == "state_machine"

sm = d.dig("runtime", "state_machine")
abort "state_machine.version must be 1" unless sm["version"] == 1
abort "graph_mode must be acyclic" unless sm["graph_mode"] == "acyclic"

nodes = sm["nodes"]
abort "no nodes" if nodes.nil? || nodes.empty?

indeg = Hash[nodes.keys.map { |k| [k, 0] }]
edges = Hash[nodes.keys.map { |k| [k, []] }]
nodes.each do |nid, n|
  abort "node #{nid} has no display_name" if n["display_name"].to_s.empty?
  abort "node #{nid} has no instruction" if n["instruction"].to_s.empty?
  abort "node #{nid} has unsupported kind #{n['kind']}" unless %w[bot_task human_input].include?(n["kind"])
  (n["transitions"] || {}).each_key do |outcome|
    abort "node #{nid} uses non-complete transition #{outcome}" unless outcome == "complete"
  end
  targets = n.dig("transitions", "complete", "targets") || []
  targets.each do |tgt|
    abort "node #{nid} targets unknown node #{tgt}" unless nodes.key?(tgt)
    indeg[tgt] += 1
    edges[nid] << tgt
  end
end

entries = indeg.select { |_, v| v.zero? }.keys
finals = nodes.select { |_, v| v["final_output"] }.keys
abort "expected exactly one entry node, got #{entries.inspect}" unless entries.size == 1
abort "expected exactly one final node, got #{finals.inspect}" unless finals.size == 1
final = finals[0]
abort "final node must have no transitions" if nodes[final].key?("transitions")
nodes.each do |nid, n|
  next if nid == final
  targets = n.dig("transitions", "complete", "targets") || []
  abort "non-final node #{nid} has no targets" if targets.empty?
end

# 入口可达全部节点，且每个节点都能到达终点
reach = ->(start, graph) {
  seen = {}
  stack = [start]
  while (cur = stack.pop)
    next if seen[cur]
    seen[cur] = true
    graph[cur].each { |nxt| stack << nxt }
  end
  seen.keys
}
from_entry = reach.call(entries[0], edges)
abort "unreachable from entry: #{(nodes.keys - from_entry).inspect}" unless (nodes.keys - from_entry).empty?
rev = Hash[nodes.keys.map { |k| [k, []] }]
edges.each { |src, tgts| tgts.each { |t| rev[t] << src } }
to_final = reach.call(final, rev)
abort "cannot reach final: #{(nodes.keys - to_final).inspect}" unless (nodes.keys - to_final).empty?

humans = nodes.select { |_, v| v["kind"] == "human_input" }
abort "at most one human_input node is supported, got #{humans.size}" if humans.size > 1
humans.each do |nid, h|
  abort "#{nid}: human_input must not set assignee/max_attempts/final_output" if
    h.key?("assignee") || h.key?("max_attempts") || h.key?("final_output")
  abort "#{nid}: human_input needs an explicit positive node_timeout_ms" unless
    h["node_timeout_ms"].is_a?(Integer) && h["node_timeout_ms"] > 0
end

nodes.each do |nid, n|
  next unless n["kind"] == "bot_task"
  abort "#{nid}: assignee.type must be bot_binding" unless n.dig("assignee", "type") == "bot_binding"
  abort "#{nid}: assignee.binding missing" if n.dig("assignee", "binding").to_s.empty?
end
parts = d["participants"].keys.sort
used = nodes.values.select { |n| n["kind"] == "bot_task" }.map { |n| n.dig("assignee", "binding") }.uniq.sort
abort "participants #{parts.inspect} != bot bindings #{used.inspect}" unless used == parts
d["participants"].each do |name, p|
  abort "participant #{name} has extra keys" unless (p.keys - %w[display_name description required extensions]).empty?
end

if kind == "speak"
  order = nodes.keys.grep(/\Aspeak_/).sort_by { |k| k.split("_").last.to_i }
  abort "speak run has no speaker nodes" if order.empty?
  abort "speak entry must be the first speaker" unless entries[0] == order[0]
  abort "speak final must be collect" unless final == "collect"
  order.each_with_index do |nid, i|
    got = nodes[nid].dig("transitions", "complete", "targets")
    want = order[(i + 1)..] + ["collect"]
    abort "speaker #{nid} is not transitively closed: #{got.inspect} != #{want.inspect}" unless got == want
  end
else
  voters = nodes.keys.grep(/\Avote_\d+\z/).sort_by { |k| k.split("_").last.to_i }
  abort "vote run has no voter nodes" if voters.empty?
  abort "vote entry must be vote_open" unless entries[0] == "vote_open"
  abort "vote final must be tally" unless final == "tally"
  got = nodes["vote_open"].dig("transitions", "complete", "targets")
  abort "vote_open must fan out to every voter: #{got.inspect} != #{voters.inspect}" unless got == voters
  voters.each do |nid|
    tgt = nodes[nid].dig("transitions", "complete", "targets")
    abort "voter #{nid} must join at tally, got #{tgt.inspect}" unless tgt == ["tally"]
  end
end

puts "#{File.basename(path)} (#{kind}): OK"
