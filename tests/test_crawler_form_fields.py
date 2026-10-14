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
