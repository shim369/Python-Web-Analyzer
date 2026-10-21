import pytest

from web_analyzer.core.crawler import WebCrawler

# osaka-midorinosato.com/contact の構造を簡略化したもの
FORM_HTML = """
<form action="/confirm" class="h-adr" method="post" id="form">
  <span class="p-country-name" style="display:none;">Japan</span>
  <ul class="form_wrapper">
    <li class="form_item">
      <div class="head_box">
        <p class="head">お名前</p><p class="must">必須</p>
        <input type="hidden" name="info[your_name][head]" value="お名前">
      </div>
      <div class="element_box"><div class="input_wrap">
        <input type="text" name="info[your_name][element]">
      </div></div>
    </li>
    <li class="form_item">
      <div class="head_box">
        <p class="head">事業選択</p>
        <input type="hidden" name="info[radio][head]" value="事業選択">
      </div>
      <div class="element_box"><div class="input_wrap"><div class="radio">
        <div class="item">
          <div class="radio_ttl"><h3>特別養護老人ホーム</h3></div>
          <div class="content">
            <label><input type="radio" name="info[radio][element]" value="a"><span>施設入所について</span></label>
          </div>
        </div>
        <div class="item">
          <div class="radio_ttl"><h3>障がい者支援施設</h3></div>
          <div class="content">
            <label><input type="radio" name="info[radio][element]" value="b"><span>就労支援事業について</span></label>
          </div>
        </div>
      </div></div></div>
    </li>
    <li class="form_item">
      <div class="head_box">
        <p class="head">お問い合わせ内容</p>
        <input type="hidden" name="info[textarea][head]" value="お問い合わせ内容">
      </div>
      <div class="element_box"><div class="input_wrap">
        <textarea name="info[textarea][element]"></textarea>
      </div></div>
    </li>
  </ul>
</form>
"""


def test_hidden_input_in_label_box_and_grouped_radios() -> None:
    fields, _ = WebCrawler()._extract_form_fields(FORM_HTML, "https://example.com/contact")
    assert fields.split("\n") == ["お名前", "事業選択", "お問い合わせ内容"]


# k-jinken.or.jp/contact の構造を簡略化したもの
BRANCH_FORM_HTML = """
<form class="h-adr" id="contact" method="post">
  <table class="tbl"><tbody>
    <tr><th>お名前<br><p class="bg-orange">必須</p></th><td><input type="text" name="name"></td></tr>
    <tr><th>フリガナ<p class="bg-orange">必須</p></th><td>
      <input type="text" name="kana">
      <div class="bg-gray flex">
        <label><input type="radio" name="who" value="協力会の会員">協力会の会員</label>
        <label><input type="radio" name="who" value="その他">その他</label>
      </div>
    </td></tr>
    <tr><th>メールアドレス<p class="bg-blue">任意</p></th><td><input type="email" name="email"></td></tr>
    <tr><th>都道府県<br>市区郡・町村<p class="bg-orange">必須</p></th><td><input type="text" name="prefectures"></td></tr>
    <tr><th>丁目・番地<br class="sp-none"><span>・</span>号・ビル名</th><td><input type="text" name="building"></td></tr>
    <tr><th>お名前<br>（全角）</th><td><input type="text" name="name2"></td></tr>
    <tr><th>個人情報の<br>取り扱いについて</th><td><input type="checkbox" name="agree"></td></tr>
    <tr><th>ご依頼内容</th><td>
      <label><input type="radio" name="content" value="見積">見積書が欲しい</label>
      <label><input type="radio" name="content" value="注文">注文したい</label>
    </td></tr>
  </tbody></table>
  <table class="tbl branch branch1 is-hidden"><tbody>
    <tr><th>見積書の宛名</th><td><input type="text" name="atena"></td></tr>
  </tbody></table>
</form>
"""


def test_branch_form_with_hidden_tables_badges_and_line_breaks() -> None:
    """- ご依頼内容で表示を切り替える非表示の分岐(class="is-hidden")の項目は数えない
    - 「任意」のバッジは項目名に含めない
    - <br>で並べた項目名は別々の項目にする(「（全角）」等の補足や、文の途中の改行は連結する)
    - 見出しをテキスト欄が使い、見出しの無い選択肢が同じセルにある場合は、選択肢名を項目にする
    """
    fields, _ = WebCrawler()._extract_form_fields(BRANCH_FORM_HTML, "https://example.com/contact")
    assert fields.split("\n") == [
        "お名前",
        "フリガナ",
        "協力会の会員",
        "その他",
        "メールアドレス",
        "都道府県",
        "市区郡・町村",
        "丁目・番地",
        "号・ビル名",
        "お名前（全角）",
        "個人情報の取り扱いについて",
        "ご依頼内容",
    ]


# meidoh.co.jp/inq の構造を簡略化したもの(項目名のセルと入力欄のセルの間に「必須」「任意」のセルがある)
BADGE_CELL_FORM_HTML = """
<form method="post">
  <p>※販促関連の案内はお断りしております。返信もいたしかねますのでご了承ください。</p>
  <table><tbody>
    <tr><td class="col_01">お名前</td><td class="col_02"><p class="must">必須</p></td><td class="col_03"><input type="text" name="name"></td></tr>
    <tr><td class="col_01">会社名</td><td class="col_02"><p class="option">任意</p></td><td class="col_03"><input type="text" name="company"></td></tr>
    <tr><td class="col_01">お問い合わせ内容</td><td class="col_02"><p class="must">必須</p></td><td class="col_03"><textarea name="inquiry"></textarea></td></tr>
  </tbody></table>
</form>
"""

# miyama-giken.co.jp/contact の構造を簡略化したもの(項目名と入力欄の間に「必須」のバッジがある)
BADGE_DIV_FORM_HTML = """
<form class="wpcf7-form" method="post">
  <div class="form-group"><label class="form-label" for="company">会社名</label>
    <span class="wpcf7-form-control-wrap"><input type="text" name="company" id="company"></span></div>
  <div class="form-group"><label class="form-label">お名前</label><div class="required-badge">必須</div>
    <span class="wpcf7-form-control-wrap"><input type="text" name="input-name"></span></div>
  <div class="form-group"><label class="form-label">都道府県</label><div class="required-badge">必須</div>
    <span class="wpcf7-form-control-wrap"><select name="prefecture"><option>北海道</option></select></span></div>
  <div class="form-group"><label class="form-label">内容</label><div class="required-badge">必須</div>
    <span class="wpcf7-form-control-wrap"><textarea name="message"></textarea></span></div>
</form>
"""


def test_skips_required_badge_between_label_and_input() -> None:
    """項目名と入力欄の間にある「必須」「任意」だけの要素は読み飛ばし、その手前を項目名にする。"""
    crawler = WebCrawler()
    fields, _ = crawler._extract_form_fields(BADGE_CELL_FORM_HTML, "https://example.com/inq")
    assert fields.split("\n") == ["お名前", "会社名", "お問い合わせ内容"]
    fields, _ = crawler._extract_form_fields(BADGE_DIV_FORM_HTML, "https://example.com/contact/")
    assert fields.split("\n") == ["会社名", "お名前", "都道府県", "内容"]


def test_keeps_confirmation_email_field_but_not_buttons() -> None:
    """確認用のメールアドレス欄は項目にする。「確認画面へ」等のボタンの文言は項目にしない。"""
    html = """
    <form><table>
      <tr><th>E-mail</th><td><input type="text" name="mail"></td></tr>
      <tr><th>E-mail(確認用)</th><td><input type="text" name="mail-confirm"></td></tr>
      <tr><th>お問い合わせ内容</th><td><textarea name="body"></textarea></td></tr>
      <tr><th>確認画面へ</th><td><input type="checkbox" name="go"></td></tr>
    </table></form>
    """
    fields, _ = WebCrawler()._extract_form_fields(html, "https://example.com/contact/")
    assert fields.split("\n") == ["E-mail", "E-mail(確認用)", "お問い合わせ内容"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 括弧の種類を問わず、囲まれた「必須」「任意」は消す
        *[(f"会社名{o}任意{c}", "会社名") for o, c in ["【】", "［］", "[]", "（）", "()", "〔〕", "〈〉", "《》", "「」", "『』", "{}", "｛｝", "<>", "＜＞"]],
        *[(f"お名前{o}必須{c}", "お名前") for o, c in ["【】", "［］", "（）", "〈〉", "《》", "「」", "<>"]],
        ("【必須項目】お名前", "お名前"),
        ("お名前（任意項目）", "お名前"),
        ("Company (optional)", "Company"),
        ("Name (Required)", "Name"),
        # 記号付き・項目名の端にある「任意」
        ("※任意 会社名", "会社名"),
        ("会社名＊任意", "会社名"),
        ("会社名 任意", "会社名"),
        ("任意：会社名", "会社名"),
        # 語句の一部の「任意」や、マーク以外の括弧書きは消さない
        ("任意団体名", "任意団体名"),
        ("任意保険の加入", "任意保険の加入"),
        ("メールアドレス（確認用）", "メールアドレス（確認用）"),
    ],
)
def test_remove_required_and_optional_marks(text: str, expected: str) -> None:
    assert WebCrawler()._remove_required_marks(text) == expected


def test_skips_optional_badge_in_brackets() -> None:
    html = """
    <form><table>
      <tr><th>会社名<span class="opt">【任意】</span></th><td><input type="text" name="company"></td></tr>
      <tr><th>お名前</th><td><span>&lt;必須&gt;</span></td><td><input type="text" name="name"></td></tr>
      <tr><th>お問い合わせ内容《必須》</th><td><textarea name="body"></textarea></td></tr>
    </table></form>
    """
    fields, _ = WebCrawler()._extract_form_fields(html, "https://example.com/contact/")
    assert fields.split("\n") == ["会社名", "お名前", "お問い合わせ内容"]
