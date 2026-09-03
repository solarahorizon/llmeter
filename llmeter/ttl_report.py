"""Render a TTL measurement as a standalone HTML page.

The terminal report states the verdict; this states the reasoning. A reader who
has never thought about prompt caching gets the mechanism — what a prefix is,
which pauses matter, why a premium charged on writes is repaid only on reads —
next to their own measured figures, so the numbers arrive already explained.

The page is one file with no external reference: styles inline, no script, no
image, no font URL. It has to open from a ``file://`` path on a machine with no
network, and it has to survive being mailed to someone as an attachment.

``render_html`` takes the same ``report`` mapping ``ttl.measure`` returns and the
same ``settings`` mapping ``ttl.current_settings`` returns, so the page and the
terminal output can never disagree about a number.
"""

import html

from llmeter import ttl

# Percentage width of the widest bar in a bucket's comparison. The other bar is
# drawn in proportion to it, so the pair reads as a ratio rather than two
# unrelated lengths.
_BAR_FULL = 100.0


def _esc(value):
    return html.escape(str(value), quote=True)


def _millions(tokens):
    return "%.1fM" % (tokens / 1e6)


_STYLE = """
:root {
  --ground:#F5F7F9; --surface:#FFFFFF; --sunken:#ECF0F4;
  --ink:#16202B; --muted:#5F6E7D; --faint:#8797A6;
  --rule:#DCE3EA; --rule-firm:#C3CFDA;
  --cost:#A75B06; --cost-bg:#FBEEDF;
  --save:#0C7361; --save-bg:#DFF1ED;
  --carry:#7C8B9A; --carry-bg:#E3E9EF;
  --accent:#234A6B;
  --shadow:0 1px 2px rgba(22,32,43,.05), 0 6px 20px rgba(22,32,43,.05);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0E141A; --surface:#151D25; --sunken:#1B242D;
    --ink:#E3EAF1; --muted:#9AA9B8; --faint:#74848F;
    --rule:#26313B; --rule-firm:#33404C;
    --cost:#E39A4E; --cost-bg:#33261A;
    --save:#45C0A8; --save-bg:#133029;
    --carry:#8395A5; --carry-bg:#232E38;
    --accent:#8FB6D8;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25);
  }
}
:root[data-theme="dark"] {
  --ground:#0E141A; --surface:#151D25; --sunken:#1B242D;
  --ink:#E3EAF1; --muted:#9AA9B8; --faint:#74848F;
  --rule:#26313B; --rule-firm:#33404C;
  --cost:#E39A4E; --cost-bg:#33261A;
  --save:#45C0A8; --save-bg:#133029;
  --carry:#8395A5; --carry-bg:#232E38;
  --accent:#8FB6D8;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25);
}
* { box-sizing:border-box; }
body { margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.6; }
.wrap { max-width:940px; margin:0 auto; padding:56px 24px 96px;
  display:flex; flex-direction:column; gap:56px; }
h1,h2,h3 { margin:0; text-wrap:balance; letter-spacing:-.015em; }
h1 { font-size:2.3rem; line-height:1.15; font-weight:640; }
h2 { font-size:1.32rem; font-weight:620; }
h3 { font-size:1rem; font-weight:620; }
p { margin:0; max-width:66ch; }
.lede { font-size:1.08rem; color:var(--muted); max-width:62ch; }
.eyebrow { font-family:var(--mono); font-size:.705rem; letter-spacing:.13em;
  text-transform:uppercase; color:var(--faint); margin:0; }
.mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
section { display:flex; flex-direction:column; gap:20px; }
.sec-head { display:flex; flex-direction:column; gap:6px; }
.note { font-size:.875rem; color:var(--muted); max-width:66ch; }
header { display:flex; flex-direction:column; gap:14px; }
.thesis { border-left:3px solid var(--accent); padding:2px 0 2px 18px;
  font-size:1.1rem; line-height:1.5; max-width:60ch; }

.answers { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }
.answer { border:1px solid var(--rule); border-radius:10px; background:var(--surface);
  padding:22px; display:flex; flex-direction:column; gap:14px; box-shadow:var(--shadow); }
.answer .who { display:flex; flex-direction:column; gap:2px; }
.answer .setting { font-family:var(--mono); font-size:.74rem; color:var(--faint); }
.callout { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.callout .pick { font-family:var(--mono); font-size:1.7rem; font-weight:640;
  color:var(--save); letter-spacing:-.02em; }
.callout .by { font-size:.9rem; color:var(--muted); }
.mismatch { font-size:.84rem; color:var(--cost); background:var(--cost-bg);
  border:1px solid var(--cost); border-radius:7px; padding:8px 11px; }
.agrees { font-size:.84rem; color:var(--muted); }
.scale { display:flex; flex-direction:column; gap:9px; }
.meter { display:flex; flex-direction:column; gap:5px; }
.meter .cap { display:flex; justify-content:space-between; font-family:var(--mono);
  font-size:.74rem; color:var(--muted); }
.meter .cap b { color:var(--ink); font-weight:500; }
.track { height:12px; border-radius:3px; background:var(--sunken); overflow:hidden; }
.fill { height:100%; border-radius:3px; }
.fill-cost { background:var(--cost); }
.fill-save { background:var(--save); }
.answer .margin { font-size:.85rem; color:var(--muted); border-top:1px solid var(--rule);
  padding-top:12px; }

.anatomy { display:flex; flex-direction:column; gap:10px; background:var(--surface);
  border:1px solid var(--rule); border-radius:10px; padding:22px; box-shadow:var(--shadow); }
.bar { display:flex; height:46px; border-radius:5px; overflow:hidden; }
.seg { display:flex; align-items:center; justify-content:center;
  font-family:var(--mono); font-size:.78rem; color:#fff; }
.seg-prefix { background:var(--carry); flex:1 1 86%; }
.seg-delta { background:var(--cost); flex:1 1 14%; }
.barkeys { display:flex; font-size:.85rem; color:var(--muted); }
.barkeys > div { padding-right:14px; }
.barkeys .k1 { flex:1 1 86%; }
.barkeys .k2 { flex:1 1 14%; }
.chips { display:flex; flex-wrap:wrap; gap:10px; }
.chip { display:inline-flex; align-items:center; gap:8px; font-family:var(--mono);
  font-size:.76rem; padding:5px 11px 5px 9px; border-radius:999px;
  border:1px solid var(--rule-firm); background:var(--surface); color:var(--muted); }
.swatch { width:11px; height:11px; border-radius:3px; flex:none; }
.sw-carry { background:var(--carry); }
.sw-cost { background:var(--cost); }
.sw-save { background:var(--save); }

.tl-scroll { overflow-x:auto; padding-bottom:4px; }
.timeline { min-width:620px; display:grid; grid-template-columns:116px repeat(4,1fr);
  gap:0 10px; align-items:stretch; }
.tl-rowlabel { display:flex; flex-direction:column; justify-content:center;
  font-family:var(--mono); font-size:.72rem; letter-spacing:.09em;
  text-transform:uppercase; color:var(--faint); padding-right:8px; }
.tl-rowlabel b { display:block; color:var(--ink); letter-spacing:-.01em;
  text-transform:none; font-family:var(--sans); font-size:.92rem; }
.tl-cell { padding:9px 0; }
.tl-head { font-family:var(--mono); font-size:.74rem; color:var(--muted);
  border-bottom:1px solid var(--rule); padding-bottom:8px; }
.tl-head b { color:var(--ink); }
.stack { display:flex; flex-direction:column; gap:3px; }
.blk { border-radius:4px; padding:7px 9px; font-family:var(--mono);
  font-size:.72rem; line-height:1.35; border:1px solid transparent; }
.blk-cost { background:var(--cost-bg); color:var(--cost); border-color:var(--cost); }
.blk-save { background:var(--save-bg); color:var(--save); border-color:var(--save); }
.blk .rate { display:block; opacity:.78; font-size:.68rem; }
.tl-gapnote { grid-column:1 / -1; display:flex; align-items:center; gap:10px;
  font-family:var(--mono); font-size:.74rem; color:var(--muted);
  border-top:1px dashed var(--rule-firm); border-bottom:1px dashed var(--rule-firm);
  padding:9px 0; margin:6px 0; }
.tl-gapnote .pause { background:var(--sunken); border:1px solid var(--rule);
  border-radius:999px; padding:2px 12px; color:var(--ink); }

.rates { border-collapse:collapse; width:100%; font-size:.92rem; }
.rates caption { text-align:left; color:var(--muted); font-size:.85rem; padding-bottom:10px; }
.rates th, .rates td { text-align:left; padding:9px 14px 9px 0;
  border-bottom:1px solid var(--rule); }
.rates th { font-family:var(--mono); font-size:.72rem; letter-spacing:.1em;
  text-transform:uppercase; color:var(--faint); font-weight:500; }
.rates td.num { font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }
.rates tr.hl td { background:var(--save-bg); }
.rates tr:last-child td { border-bottom:none; }

.bands { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.band { border:1px solid var(--rule); border-radius:9px; padding:16px;
  background:var(--surface); display:flex; flex-direction:column; gap:7px; }
.band.counts { border-color:var(--save); background:var(--save-bg); }
.band .len { font-family:var(--mono); font-size:.95rem; font-weight:600; }
.band .verdict { font-size:.82rem; color:var(--muted); }
.band.counts .verdict { color:var(--save); font-weight:600; }

.ledger { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }
.side { border:1px solid var(--rule); border-radius:10px; background:var(--surface);
  padding:20px; display:flex; flex-direction:column; gap:12px; box-shadow:var(--shadow); }
.side h3 { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; }
.side .tag { font-family:var(--mono); font-size:.7rem; letter-spacing:.1em;
  text-transform:uppercase; color:var(--faint); }
.side .because { font-size:.84rem; color:var(--muted); }
.calc { display:flex; flex-direction:column; gap:6px; font-family:var(--mono);
  font-size:.84rem; font-variant-numeric:tabular-nums; }
.calc .line { display:flex; justify-content:space-between; gap:12px; color:var(--muted); }
.calc .line b { color:var(--ink); font-weight:500; }
.calc .total { display:flex; justify-content:space-between; gap:12px;
  border-top:1px solid var(--rule-firm); padding-top:8px; margin-top:2px;
  font-size:1.02rem; color:var(--ink); }
.verdictbar { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px;
  border:1px solid var(--save); background:var(--save-bg); border-radius:10px;
  padding:16px 20px; }
.verdictbar .big { font-family:var(--mono); font-size:1.2rem; font-weight:620; color:var(--save); }
.verdictbar .rest { font-size:.93rem; color:var(--ink); }
.empty { border:1px dashed var(--rule-firm); border-radius:10px; padding:20px;
  color:var(--muted); font-size:.9rem; }

.caveats { display:flex; flex-direction:column; }
.caveats .row { display:grid; grid-template-columns:160px minmax(0,1fr); gap:18px;
  padding:13px 0; border-top:1px solid var(--rule); font-size:.9rem; color:var(--muted); }
.caveats .row:last-child { border-bottom:1px solid var(--rule); }
.caveats .term { font-family:var(--mono); font-size:.74rem; letter-spacing:.07em;
  text-transform:uppercase; color:var(--ink); padding-top:2px; }

footer { color:var(--faint); font-size:.8rem; font-family:var(--mono);
  border-top:1px solid var(--rule); padding-top:18px; }

@media (max-width:760px) {
  .answers, .bands, .ledger { grid-template-columns:minmax(0,1fr); }
  .caveats .row { grid-template-columns:minmax(0,1fr); gap:4px; }
  h1 { font-size:1.8rem; }
}
@media (prefers-reduced-motion: reduce) { * { animation:none !important; transition:none !important; } }
"""


# Everything from here to _CAVEATS is fixed prose: the mechanism, which is the
# same for every reader. Only the figures around it are measured.
_ANATOMY = """
<section>
  <div class="sec-head">
    <p class="eyebrow">how to read it &middot; 1</p>
    <h2>Every message re-sends the whole conversation</h2>
    <p class="note">The model keeps nothing between calls, so the transcript goes up in full on
    every turn. It splits into two very unequal parts, and the cache exists for the larger one.</p>
  </div>
  <div class="anatomy">
    <div class="bar">
      <div class="seg seg-prefix">prefix</div>
      <div class="seg seg-delta">delta</div>
    </div>
    <div class="barkeys">
      <div class="k1"><b>Prefix</b> &mdash; everything said so far, identical to last time.</div>
      <div class="k2"><b>Delta</b> &mdash; the new turn.</div>
    </div>
  </div>
  <p class="note">A token is written to the cache once, on the turn it first appears as delta.
  From the next turn on it is part of the prefix and is only ever read. So each token pays the
  write surcharge exactly once, then rides cheap &mdash; until a pause outlives the cache.</p>
  <div class="chips">
    <span class="chip"><span class="swatch sw-carry"></span>prefix &mdash; already in the conversation</span>
    <span class="chip"><span class="swatch sw-cost"></span>written &mdash; costs more than plain input</span>
    <span class="chip"><span class="swatch sw-save"></span>read &mdash; costs a tenth</span>
  </div>
</section>
"""

_TIMELINE = """
<section>
  <div class="sec-head">
    <p class="eyebrow">how to read it &middot; 2</p>
    <h2>One conversation, one coffee break</h2>
    <p class="note">Four turns. The first three arrive quickly; then a twenty minute pause.
    Follow the last column &mdash; it is the only place the two lifetimes behave differently.</p>
  </div>
  <div class="tl-scroll">
    <div class="timeline">
      <div class="tl-rowlabel"></div>
      <div class="tl-head">turn 1 &mdash; <b>09:00</b></div>
      <div class="tl-head">turn 2 &mdash; <b>09:02</b></div>
      <div class="tl-head">turn 3 &mdash; <b>09:03</b></div>
      <div class="tl-head">turn 4 &mdash; <b>09:23</b></div>
      <div class="tl-gapnote">
        <span>turns arrive a couple of minutes apart</span>
        <span class="pause">&nbsp;&mdash; then a 20 minute pause &mdash;&nbsp;</span>
      </div>
      <div class="tl-rowlabel"><b>With 1h</b><span>cache still alive</span></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-cost">write prefix<span class="rate">2.0&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-save">read prefix<span class="rate">0.1&times;</span></div>
        <div class="blk blk-cost">write delta<span class="rate">2.0&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-save">read prefix<span class="rate">0.1&times;</span></div>
        <div class="blk blk-cost">write delta<span class="rate">2.0&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-save">read prefix<span class="rate">0.1&times; &mdash; survived the pause</span></div>
        <div class="blk blk-cost">write delta<span class="rate">2.0&times;</span></div></div></div>
      <div class="tl-rowlabel"><b>With 5m</b><span>cache expired</span></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-cost">write prefix<span class="rate">1.25&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-save">read prefix<span class="rate">0.1&times;</span></div>
        <div class="blk blk-cost">write delta<span class="rate">1.25&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-save">read prefix<span class="rate">0.1&times;</span></div>
        <div class="blk blk-cost">write delta<span class="rate">1.25&times;</span></div></div></div>
      <div class="tl-cell"><div class="stack">
        <div class="blk blk-cost">re-write prefix<span class="rate">1.25&times; &mdash; it expired</span></div>
        <div class="blk blk-cost">write delta<span class="rate">1.25&times;</span></div></div></div>
    </div>
  </div>
  <p class="note">Turns 1&ndash;3 differ only in the write rate, where 5m is cheaper every time.
  Turn 4 is where 1h collects: it reads a prefix that 5m has to buy again.
  <b>That rescued prefix is the entire case for the hour.</b></p>
</section>
"""

_RATES = """
<section>
  <div class="sec-head">
    <p class="eyebrow">how to read it &middot; 3</p>
    <h2>Four rates, all relative to plain input</h2>
  </div>
  <table class="rates">
    <caption>Published cache pricing as a multiple of the model&rsquo;s ordinary input price.</caption>
    <thead><tr><th scope="col">What happens to a token</th><th scope="col">Rate</th>
      <th scope="col">Meaning</th></tr></thead>
    <tbody>
      <tr><td>Sent as ordinary input</td><td class="num">1.00&times;</td><td>the baseline</td></tr>
      <tr><td>Written into the 5m cache</td><td class="num">1.25&times;</td><td>a surcharge to store it</td></tr>
      <tr><td>Written into the 1h cache</td><td class="num">2.00&times;</td><td>double, to store it longer</td></tr>
      <tr class="hl"><td>Read back out of either cache</td><td class="num">0.10&times;</td>
        <td>a tenth &mdash; the whole point</td></tr>
    </tbody>
  </table>
  <p class="note">So 1h costs an extra <b class="mono">0.75&times;</b> on every token written, and
  saves <b class="mono">1.15&times;</b> on every prefix token it rescues &mdash; the
  <span class="mono">1.25</span> that would have been re-written, less the
  <span class="mono">0.10</span> still paid to read it. A model that reads cheaper saves a
  little more; the ledgers below use the read rate your own traffic got.</p>
</section>
"""

_BANDS = """
<section>
  <div class="sec-head">
    <p class="eyebrow">how to read it &middot; 4</p>
    <h2>Only breaks between 5 and 60 minutes decide anything</h2>
  </div>
  <div class="bands">
    <div class="band"><span class="len">under 5 min</span>
      <span class="verdict">Both caches alive, both read. No difference &mdash; cancels out.</span></div>
    <div class="band counts"><span class="len">5 &ndash; 60 min</span>
      <span class="verdict">1h reads, 5m re-writes. The only band that counts.</span></div>
    <div class="band"><span class="len">over 60 min</span>
      <span class="verdict">Both caches dead, both re-write. No difference &mdash; cancels out.</span></div>
  </div>
  <p class="note">Most of your pauses are therefore irrelevant to the choice. A short think and a
  long lunch both cancel; the middling break does not.</p>
</section>
"""

_CAVEATS = """
<section>
  <div class="sec-head">
    <p class="eyebrow">where the estimate is soft</p>
    <h2>Read it as a direction, not a bill</h2>
  </div>
  <div class="caveats">
    <div class="row"><span class="term">Quota, not dollars</span>
      <span>On a subscription you spend quota. How the meter weights a 1h write against a 5m one is
      not derivable from a transcript, so figures are input-token equivalents.</span></div>
    <div class="row"><span class="term">Fused misses</span>
      <span>Where the cache missed, the transcript reports prefix and delta as one number. The split
      uses that conversation&rsquo;s own short-gap turns as the sample. Post-pause turns tend to be
      bigger, so this leans mildly toward 1h.</span></div>
    <div class="row"><span class="term">Dropped prefixes</span>
      <span>A gap whose prefix was already gone &mdash; after a compaction or a model switch &mdash;
      is counted as though 1h would have held it. Flatters 1h a little.</span></div>
    <div class="row"><span class="term">Your week</span>
      <span>A quieter week lowers writes and raises breaks. Where the margin is thin the window
      itself can change the answer, so re-run when your working pattern changes.</span></div>
  </div>
</section>
"""


def _answer_card(name, stats, value, source):
    """The verdict for one bucket: the pick, the two bars, and the margin."""
    head = (
        '<div class="answer">\n'
        '  <div class="who"><h3>%s</h3><span class="setting">%s</span></div>\n'
        % (_esc(name.capitalize()), _esc(stats["setting"]))
    )
    if stats["requests"] == 0:
        return head + '  <p class="empty">No requests in this window.</p>\n</div>'

    parts = [head]
    parts.append(
        '  <div class="callout"><span class="pick">use %s</span>'
        '<span class="by">cheaper by %s</span></div>\n'
        % (_esc(stats["verdict"]), _esc(_millions(abs(stats["delta"]))))
    )

    widest = max(stats["premium_1h"], stats["penalty_5m"]) or 1.0
    for label, amount, colour in (
        ("1h pays extra", stats["premium_1h"], "cost"),
        ("5m pays extra", stats["penalty_5m"], "save"),
    ):
        parts.append(
            '  <div class="meter"><div class="cap"><span>%s</span><b>%s</b></div>'
            '<div class="track"><div class="fill fill-%s" style="width:%.1f%%"></div></div></div>\n'
            % (label, _esc(_millions(amount)), colour, _BAR_FULL * amount / widest)
        )

    if value and value != stats["verdict"]:
        parts.append(
            '  <p class="mismatch">Your setting is <b>%s</b>. This measurement points the other way.</p>\n'
            % _esc(value)
        )
    elif value:
        parts.append(
            '  <p class="agrees">Your setting is <b>%s</b>, which agrees. Nothing to change.</p>\n'
            % _esc(value)
        )
    else:
        parts.append(
            '  <p class="agrees">Not set in %s, so Claude Code&rsquo;s default applies.</p>\n'
            % _esc(source)
        )

    margin = (
        "%s requests across %s conversations. %s written to cache, %s of prefix rescued "
        "across %d breaks of 5&ndash;60 minutes."
        % (
            "{:,}".format(stats["requests"]),
            "{:,}".format(stats["conversations"]),
            _millions(stats["write_tokens"]),
            _millions(stats["gap_tokens"]),
            stats["gap_count"],
        )
    )
    if stats["flip_factor"]:
        moved = "break volume" if stats["verdict"] == "5m" else "write volume"
        margin += " Your %s would have to be %.2f&times; what it is to flip this." % (
            moved,
            stats["flip_factor"],
        )
    parts.append('  <p class="margin">%s</p>\n</div>' % margin)
    return "".join(parts)


def _ledger(name, stats):
    """The arithmetic behind one bucket's verdict, both sides shown."""
    if stats["requests"] == 0:
        return ""
    estimated = ""
    if stats["gaps_estimated"]:
        estimated = (
            " %d of those breaks had a fused figure the tool had to split."
            % stats["gaps_estimated"]
        )
    return """
<section>
  <div class="sec-head">
    <p class="eyebrow">the arithmetic &middot; %s</p>
    <h2>What it costs, against what it saves</h2>
    <p class="note">The two totals do not overlap: for any one request, its written tokens land in
    the left column and its rescued prefix in the right.%s</p>
  </div>
  <div class="ledger">
    <div class="side">
      <h3>Cache writes <span class="tag">what 1h costs</span></h3>
      <p class="because">Every token written to cache in the window, the delta on post-break turns
      included, because both lifetimes write that. The 1h premium is charged on all of it, whether
      you take breaks or not.</p>
      <div class="calc">
        <div class="line"><span>tokens written</span><b>%s</b></div>
        <div class="line"><span>1h surcharge over 5m</span><b>&times; 0.75</b></div>
        <div class="total"><span>1h pays extra</span><b>%s</b></div>
      </div>
    </div>
    <div class="side">
      <h3>Writes 1h avoids <span class="tag">what 1h saves</span></h3>
      <p class="because">Prefix tokens on turns that followed a 5&ndash;60 minute break. Under 1h
      they were read; under 5m they would have been written again.</p>
      <div class="calc">
        <div class="line"><span>prefix rescued</span><b>%s</b></div>
        <div class="line"><span>5m surcharge over 1h (1.25 less your %.3g read rate)</span><b>&times; %.2f</b></div>
        <div class="total"><span>5m pays extra</span><b>%s</b></div>
      </div>
    </div>
  </div>
  <div class="verdictbar">
    <span class="big">%s wins by %s</span>
    <span class="rest">Neither figure is a bill. Both are extras above a floor the two settings
    share, so only the gap between them is real.</span>
  </div>
</section>
""" % (
        _esc(name),
        estimated,
        _esc(_millions(stats["write_tokens"])),
        _esc(_millions(stats["premium_1h"])),
        _esc(_millions(stats["gap_tokens"])),
        stats["read_rate"],
        ttl.WRITE_5M - stats["read_rate"],
        _esc(_millions(stats["penalty_5m"])),
        _esc(stats["verdict"]),
        _esc(_millions(abs(stats["delta"]))),
    )


def render_html(report, settings):
    """Return the whole page as one HTML string.

    Takes what ``ttl.measure`` and ``ttl.current_settings`` return, so the page
    cannot disagree with the terminal report. Buckets with no requests render a
    short placeholder rather than being dropped, because a missing section reads
    as a bug rather than as a quiet window.
    """
    generated = report["generated_at"].astimezone()

    body = [
        '<div class="wrap">',
        "<header>",
        '  <p class="eyebrow">llmeter ttl &middot; last %d days, to %s</p>'
        % (report["window_days"], _esc(generated.strftime("%d %B %Y, %H:%M %Z"))),
        "  <h1>Which cache lifetime is cheaper</h1>",
        '  <p class="lede">Claude Code can hold your conversation in a cache for five minutes or '
        "for an hour. The hour costs more to keep. Whether it repays that is measured below, on "
        "your own transcripts.</p>",
        '  <p class="thesis">You pay the one-hour premium on <b>everything you write</b>. '
        "You earn it back only on <b>what survives a break</b>.</p>",
        "</header>",
        "<section>",
        '  <div class="sec-head"><p class="eyebrow">your answer</p>'
        "<h2>Two settings, measured separately</h2>"
        '<p class="note">Claude Code sorts every request into one of two pools. Subagents, '
        "workflows, compaction and title generation are all &ldquo;everything else&rdquo;, and "
        "that pool behaves nothing like a conversation.</p></div>",
        '  <div class="answers">',
    ]
    for name in (ttl.BUCKET_MAIN, ttl.BUCKET_OTHER):
        stats = report["buckets"][name]
        value, source = settings.get(name, (None, "unknown"))
        body.append(_answer_card(name, stats, value, source))
    body.append("  </div>")
    body.append("</section>")

    body.append(_ANATOMY)
    body.append(_TIMELINE)
    body.append(_RATES)
    body.append(_BANDS)
    for name in (ttl.BUCKET_MAIN, ttl.BUCKET_OTHER):
        body.append(_ledger(name, report["buckets"][name]))
    body.append(_CAVEATS)
    body.append(
        "<footer>llmeter ttl &mdash; measured from local transcripts under "
        "~/.claude/projects &middot; no network, nothing sent anywhere</footer>"
    )
    body.append("</div>")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Which Cache Lifetime Is Cheaper</title>\n"
        "<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n"
        % (_STYLE, "\n".join(body))
    )
