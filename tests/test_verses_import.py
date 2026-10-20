from gita_reels.verses_import import parse_discourse

HTML = """<style>.x{}</style><div class="wst-header"><p>The Bhagavad-Gita (1922) Discourse 2</p></div>
<div class="wst-block-center"><div class="wst-size-block"><div class="wst-lang" lang="sa" dir="ltr">
<p>यामिमां पुष्पितां वाचं प्रवदन्त्यविपश्चितः ।
</p><p>वेदवादरताः पार्थ नान्यदस्तीति वादिनः ॥ ४२ ॥
</p></div></div></div>
<p>Flowery speech is uttered by the foolish, rejoicing in the letter of the Vedas,<sup id="cite_ref-17" class="reference"><a href="#cite_note-17">[17]</a></sup> O P&acirc;rtha, saying: "There is naught but this"; <span class="wst-floatright"><span>(42)</span></span>
</p>
<div class="wst-block-center"><div><div class="wst-lang" lang="sa"><p>कामात्मानः स्वर्गपरा जन्मकर्मफलप्रदाम् ।
</p><p>क्रियाविशेषबहुलां भोगैश्वर्यगतिं प्रति ॥ ४३ ॥
</p></div></div></div>
<p>With desire for self, with heaven for goal, they offer birth
</p><p><span><span class="pagenum ws-pagenum" id="34">34</span></span>as the fruit of action. <span>(43)</span></p>
<p>Lines one and two of a joint verse. <span>(44-45)</span></p>
<div class="mw-references-wrap"><ol class="references"><li>17. footnote (99)</li></ol></div>"""


def test_besant_page_parses_verses_with_sanskrit_and_strips_footnotes():
    v = parse_discourse(HTML)
    assert v[42]["translation"] == 'Flowery speech is uttered by the foolish, rejoicing in the letter of the Vedas, ' \
                                   'O Pârtha, saying: "There is naught but this";'
    assert v[42]["sanskrit"].endswith("वादिनः ॥ ४२ ॥")
    assert v[43]["translation"] == "With desire for self, with heaven for goal, they offer birth as the fruit of action."
    assert v[44]["translated_with"] == "44-45" and v[45]["translation"] == v[44]["translation"]
    assert 99 not in v


def test_misprinted_or_missing_markers_fall_back_to_the_sanskrit_order():
    html = """<div lang="sa"><p>क ॥ ४१ ॥</p></div><p>Verse forty-one. <span>(41)</span></p>
<div lang="sa"><p>ख ॥ ४२ ॥</p></div><p>Verse forty-two as printed. <span title="[sic] '(42)'">(42</span></p>
<div lang="sa"><p>ग ॥ ४३ ॥</p></div><p>Verse forty-three with no marker.</p>
<div lang="sa"><p>घ ॥ ४४ ॥</p></div><p>Verse forty-four. <span>(44)</span></p>
<p>Thus in the glorious Bhagavad-Gita ends the third discourse.</p>"""
    v = parse_discourse(html)
    assert v[42]["translation"] == "Verse forty-two as printed."
    assert v[43]["translation"] == "Verse forty-three with no marker."
    assert v[44]["translation"] == "Verse forty-four." and 45 not in v
