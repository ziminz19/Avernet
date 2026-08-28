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

# 节点超时与重试。Bot 的会话通道是串行的，偶尔会在一次激活结束后没有及时释放，
# 节点任务要等 Bot 侧的自愈把通道抢回来，那是分钟级的。超时短于这个窗口，BCS 会
# 先判死，节点产物随后到达变成孤儿。重试对这类故障无效，只会让同一节点排两份。
BOT_NODE_TIMEOUT_MS = 420_000
CONTENDED_NODE_TIMEOUT_MS = 600_000
ENTRY_NODE_TIMEOUT_MS = 900_000
defaults = sm["defaults"] || {}
abort "defaults.max_attempts must be 1, got #{defaults['max_attempts'].inspect}" unless
  defaults["max_attempts"] == 1
abort "defaults.node_timeout_ms must be >= #{BOT_NODE_TIMEOUT_MS}, got #{defaults['node_timeout_ms'].inspect}" unless
  defaults["node_timeout_ms"].is_a?(Integer) && defaults["node_timeout_ms"] >= BOT_NODE_TIMEOUT_MS

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

# 流程推进只由主持人做，所以两个运行的入口节点都是裁判的 bot_task，产物就是那一轮
# 的开场稿。代价是裁判在自己的一次激活里同步提交运行，入口节点会排在那次激活后面
# 等自己让路——所以入口节点的超时必须给到 15 分钟（实测通道最长泄漏过 6 分钟）。
referee_nodes = nodes.select { |_, n| n.dig("assignee", "binding") == "referee" }.keys.sort
entry = entries[0]
abort "entry node #{entry} must be assigned to the referee" unless
  nodes[entry].dig("assignee", "binding") == "referee"
abort "entry node #{entry} needs node_timeout_ms >= #{ENTRY_NODE_TIMEOUT_MS}" unless
  nodes[entry]["node_timeout_ms"].is_a?(Integer) && nodes[entry]["node_timeout_ms"] >= ENTRY_NODE_TIMEOUT_MS

nodes.each do |nid, n|
  next unless n["kind"] == "bot_task"
  abort "#{nid}: must not raise max_attempts above 1" if n.key?("max_attempts") && n["max_attempts"] != 1
  effective = n["node_timeout_ms"] || defaults["node_timeout_ms"]
  want = if nid == entry then ENTRY_NODE_TIMEOUT_MS
         elsif n.dig("assignee", "binding") == "referee" then CONTENDED_NODE_TIMEOUT_MS
         else BOT_NODE_TIMEOUT_MS end
  abort "#{nid}: node_timeout_ms #{effective} < #{want}" unless effective.is_a?(Integer) && effective >= want
end
parts = d["participants"].keys.sort
used = nodes.values.select { |n| n["kind"] == "bot_task" }.map { |n| n.dig("assignee", "binding") }.uniq.sort
abort "participants #{parts.inspect} != bot bindings #{used.inspect}" unless used == parts
d["participants"].each do |name, p|
  abort "participant #{name} has extra keys" unless (p.keys - %w[display_name description required extensions]).empty?
end

if kind == "speak"
  order = nodes.keys.grep(/\Aspeak_\d+\z/).sort_by { |k| k.split("_").last.to_i }
  abort "speak run has no speaker nodes" if order.empty?
  abort "speak entry must be speak_open" unless entry == "speak_open"
  abort "speak final must be collect" unless final == "collect"
  abort "speak run may only assign the referee to speak_open and collect, got #{referee_nodes.inspect}" unless
    referee_nodes == %w[collect speak_open]
  got = nodes["speak_open"].dig("transitions", "complete", "targets")
  abort "speak_open must fan out to every speaker: #{got.inspect} != #{order.inspect}" unless got == order
  abort "speak_open must not carry a bluntness block" if
    nodes["speak_open"]["instruction"].to_s.include?("本轮钝度")
  # 泄词有字面和语义两条路。forbid_line 管字面，钝度段管语义——第一轮就把词描述到
  # 只对应一件东西，卧底当场暴露，整局一轮就结束。每个发言节点都必须带上本轮钝度。
  #
  # 人类那一席是短句版：Bot 节点那套（枚举二字组合、自检问句、类目白名单）是写给
  # 模型看的硬约束，人类读到一半就跳过了。所以只要求它带上「说钝一点 + 至少 N 样」
  # 这把尺子本身，形式不同、约束不缺。
  order.each do |nid|
    inst = nodes[nid]["instruction"].to_s
    if nodes[nid]["kind"] == "human_input"
      abort "human speaker #{nid} is missing the short bluntness line" unless
        inst.include?("说钝一点") && inst =~ /至少 \d+ 样/
    else
      abort "speaker #{nid} is missing the bluntness block" unless inst.include?("本轮钝度")
    end
  end
  order.each_with_index do |nid, i|
    got = nodes[nid].dig("transitions", "complete", "targets")
    want = order[(i + 1)..] + ["collect"]
    abort "speaker #{nid} is not transitively closed: #{got.inspect} != #{want.inspect}" unless got == want
  end
else
  voters = nodes.keys.grep(/\Avote_\d+\z/).sort_by { |k| k.split("_").last.to_i }
  abort "vote run has no voter nodes" if voters.empty?
  abort "vote entry must be vote_open" unless entry == "vote_open"
  abort "vote final must be tally" unless final == "tally"
  abort "vote run may only assign the referee to vote_open and tally, got #{referee_nodes.inspect}" unless
    referee_nodes == %w[tally vote_open]
  # 投票只交票号。一条理由就是「拿我的词比对他的话」的结果，念出来等于广播自己那个
  # 词的属性——第一轮结束全场信息就透明了。
  voters.each do |nid|
    text = nodes[nid]["instruction"].to_s
    abort "voter #{nid} must ask for a bare ballot" unless
      text.match?(/只输出票号|只写「我投N号」/)
    abort "voter #{nid} must not ask for a reason or a quote" if
      text.match?(/接一句|理由里必须|引用.{0,8}原话/)
  end
  got = nodes["vote_open"].dig("transitions", "complete", "targets")
  abort "vote_open must fan out to every voter: #{got.inspect} != #{voters.inspect}" unless got == voters
  voters.each do |nid|
    tgt = nodes[nid].dig("transitions", "complete", "targets")
    abort "voter #{nid} must join at tally, got #{tgt.inspect}" unless tgt == ["tally"]
  end
end

puts "#{File.basename(path)} (#{kind}): OK"
