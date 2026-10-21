import logging
import os
import re
import threading
import time

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

HTML_PARSER = "lxml"


class SiteSummarizer:
    """トップページ・会社概要ページのHTMLから社名・事業内容に関わる本文を抜き出し、
    Gemini APIで100文字前後の「サイト概要」に要約する(K列「用途」用)。

    meta descriptionやtitleには社名しか入っていないサイトが多く、
    従来は人手でGeminiに要約させてK列を入れ直していたため、それを自動化する。

    APIキー(環境変数 GEMINI_API_KEY)が未設定の場合や、API呼び出しに
    失敗した場合は空文字を返す。呼び出し側は空文字なら元の値を残す想定。
    """

    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    DEFAULT_MODEL = "gemini-3.5-flash-lite"

    # 無料枠のRPM(1分あたりリクエスト数)上限に引っかからないよう、
    # 全スレッド共通でリクエスト間隔をこの秒数以上空ける。
    # 既定の6秒 = 最大10RPM。AI Studioで確認した実際の上限に合わせて
    # 環境変数 GEMINI_MIN_INTERVAL_SEC で調整できる。
    DEFAULT_MIN_INTERVAL_SEC = 6.0

    # 429(レート制限)時の再試行回数と待機秒数
    MAX_RETRIES = 2
    RETRY_WAIT_SEC = 20.0

    # 再試行しても429が続いた場合(1日あたりの上限到達など)は、無駄な待ち時間で
    # 処理全体を遅らせないよう、この秒数のあいだ要約自体をスキップする。
    QUOTA_COOLDOWN_SEC = 600.0

    MAX_CHARS_PER_PAGE = 1500
    MAX_PROFILE_PAGES = 2

    # スレッド間で共有するレート制御用の状態
    _rate_lock = threading.Lock()
    _last_request_at = 0.0
    _cooldown_until = 0.0
    # 利用上限(429)のためにAPIを呼べなかったことを、呼び出したスレッドごとに記録する。
    # summarize()等は失敗しても空文字を返すだけのため、呼び出し側が「上限が原因か」を
    # 知るにはpop_rate_limited()で確認する(1行分の処理は1つのスレッドで完結する)。
    _thread_state = threading.local()

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout
        try:
            self.min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL_SEC", self.DEFAULT_MIN_INTERVAL_SEC))
        except ValueError:
            self.min_interval = self.DEFAULT_MIN_INTERVAL_SEC

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def pop_rate_limited(self) -> bool:
        """このスレッドで、前回の呼び出し以降に利用上限のためAPIを呼べなかったことがあるかを返し、記録を消す。"""
        limited = bool(getattr(SiteSummarizer._thread_state, "rate_limited", False))
        SiteSummarizer._thread_state.rate_limited = False
        return limited

    def summarize(self, domain: str, top_html: str, profile_htmls: list[str] | None = None, current_description: str = "") -> str:
        """サイト概要(100文字前後)を返す。要約できなかった場合は空文字。

        Args:
            domain: 対象サイトのドメイン(プロンプトにURLとして渡す)。
            top_html: トップページのHTML。
            profile_htmls: 会社概要・事業内容などのページのHTML。
            current_description: 現在K列に入っている文字列(社名だけ等でも手がかりとして渡す)。
        """
        if not self.enabled or not top_html:
            return ""

        source_text = self.build_source_text(top_html, profile_htmls or [])
        if not source_text:
            return ""

        prompt = f"URL: {domain}\n現在の説明文: {current_description or '(なし)'}\n\n--- サイト本文(抜粋) ---\n{source_text}"

        try:
            raw = self._generate(prompt)
        except Exception as e:
            logger.warning(f"[{domain}] Geminiによるサイト概要の要約に失敗しました: {e}")
            return ""

        return self._clean_output(raw)

    def build_source_text(self, top_html: str, profile_htmls: list[str]) -> str:
        """トップページと、会社概要・事業内容などのページの本文テキストを連結する。"""
        sections: list[str] = []
        top_title, top_text = self._extract_page_text(top_html)
        if top_text:
            sections.append(f"[トップページ: {top_title}]\n{top_text}")

        for page_html in profile_htmls[: self.MAX_PROFILE_PAGES]:
            title, text = self._extract_page_text(page_html)
            if text:
                sections.append(f"[{title}]\n{text}")

        return "\n\n".join(sections)

    def _extract_page_text(self, html: str) -> tuple[str, str]:
        """(ページ見出し, 本文テキスト)を返す。ナビ・フッター等の定型部分は除く。"""
        try:
            soup = BeautifulSoup(html, HTML_PARSER)
        except Exception:
            return "", ""

        title = soup.title.get_text(strip=True) if soup.title else ""
        h1 = soup.find("h1")
        h1_text = h1.get_text(strip=True) if isinstance(h1, Tag) else ""
        heading = " / ".join(filter(None, [title, h1_text]))

        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "iframe"]):
            tag.decompose()

        body = soup.body or soup
        text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
        return heading, text[: self.MAX_CHARS_PER_PAGE]

    SUMMARY_INSTRUCTION = (
        "あなたはWebサイトの調査担当者です。与えられたサイト本文から、"
        "そのサイトを運営する会社・団体の名称と事業内容(何をしている会社か)を、"
        "日本語で100文字前後の1〜2文に要約してください。"
        "「〇〇株式会社のコーポレートサイト。〜を手がける。」のような体言止め中心の簡潔な文体にしてください。"
        "本文から読み取れない事実は書かず、前置き・引用符・改行は付けず要約文だけを出力してください。"
    )

    NAV_SELECT_INSTRUCTION = (
        "あなたはWebサイトの調査担当者です。Webページ内にあるメニューのリストが番号付きで与えられます。"
        "このうち、サイトのグローバルナビゲーション(PC表示でページ上部やサイドに常に表示される、"
        "サイトの主要なページへのメニュー)にあたるリストを1つ選び、その番号だけを半角数字で出力してください。"
        "外部サイトへのリンク集、SNSリンク、お知らせ・ブログ記事の一覧、商品やカテゴリの一覧、"
        "利用規約・プライバシーポリシー等だけを並べたリスト、パンくずリストは選ばないでください。"
        "会社概要・事業内容・製品情報・採用情報・お問い合わせ等、サイトの主要ページへのリンクを複数含むリストがあれば、"
        "項目名が画像の代替テキスト由来で素朴なものでもそれを選んでください。"
        "ヘッダーには、サイトの主要なセクション(事業内容・製品情報・サービス・会社案内等)を並べたメインのメニューとは別に、"
        "右上などに「会社概要・採用情報・お問い合わせ・アクセス」のような補助的な項目を3〜4個だけ並べた小さなメニューが"
        "置かれていることがあります。両方ある場合は、項目の多いメインのメニューを選んでください。"
        "ただし、個々の製品名・商品名・カテゴリ名だけを並べたリストは、メインのメニューではありません。"
        "どれも明らかにグローバルナビではない場合に限り0を出力してください。説明や前置きは不要です。"
    )

    # select_global_nav()で、AIが「該当するリストが無い」と判定した場合の戻り値
    NO_GLOBAL_NAV = -1

    def select_global_nav(self, domain: str, candidates: list[list[str]]) -> int | None:
        """候補のメニューのリストのうち、グローバルナビにあたるものの添字(0始まり)を返す。

        AIが該当なしと判定した場合はNO_GLOBAL_NAV、API呼び出しに失敗した・回答を
        解釈できなかった場合はNone。AIには番号だけを答えさせ、項目名は候補
        (サイト上の文字そのまま)を使うことで、言い換えや存在しない項目の混入を防ぐ。
        """
        if not self.enabled or not candidates:
            return None
        listing = "\n".join(f"{i}. " + " / ".join(menus) for i, menus in enumerate(candidates, start=1))
        prompt = f"URL: {domain}\n\n--- メニューのリスト ---\n{listing}"
        try:
            raw = self._generate(prompt, self.NAV_SELECT_INSTRUCTION)
        except Exception as e:
            logger.warning(f"[{domain}] Geminiによるグローバルナビの判定に失敗しました: {e}")
            return None
        match = re.search(r"\d+", raw)
        if not match:
            return None
        number = int(match.group())
        if number == 0:
            return self.NO_GLOBAL_NAV
        return number - 1 if number <= len(candidates) else None

    CONTACT_FIELDS_INSTRUCTION = (
        "あなたはWebサイトの調査担当者です。お問い合わせページの本文が与えられます。"
        "入力欄は[入力欄]、複数行の入力欄は[複数行入力欄]、選択欄は[選択欄]、チェックボックス・ラジオボタンは[選択肢]と表記しています。"
        "ページ内のお問い合わせフォームで、利用者が入力・選択する項目の見出し(項目名)を、ページ上の表記のまま、上から順に1行に1つずつ出力してください。"
        "「必須」「任意」の表示、入力例、注意書き、選択肢の一つ一つ、送信・確認ボタン、サイト内検索やメールマガジン登録の欄は含めないでください。"
        "ただし、見出しの無いチェックボックス(例: 個人情報の取り扱いに同意する)は、その文言を項目名にしてください。"
        "お問い合わせフォームが無い(電話番号やメールアドレスの案内だけ等)場合は「なし」とだけ出力してください。説明や前置きは不要です。"
    )

    def extract_contact_fields(self, domain: str, pages: list[tuple[str, str]]) -> list[str] | None:
        """お問い合わせページの本文(URL, テキスト)から、フォームの項目名のリストを返す。

        AIがフォームは無いと判定した場合は空リスト、API呼び出しに失敗した・回答を
        解釈できなかった場合はNone。AIが言い換えた・存在しない項目を混ぜるのを防ぐため、
        ページ本文に(空白を除いて)そのまま含まれる項目名だけを採用する。
        """
        if not self.enabled or not pages:
            return None
        listing = "\n\n".join(f"[{url}]\n{text}" for url, text in pages)
        prompt = f"URL: {domain}\n\n--- お問い合わせページの本文 ---\n{listing}"
        try:
            raw = self._generate(prompt, self.CONTACT_FIELDS_INSTRUCTION)
        except Exception as e:
            logger.warning(f"[{domain}] Geminiによる問合せ項目の読み取りに失敗しました: {e}")
            return None

        lines = [re.sub(r"^\s*(?:[-*・●■]|\d+[.)．])\s*", "", line).strip() for line in raw.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return None
        if lines == ["なし"]:
            return []
        page_text = re.sub(r"\s+", "", listing)
        items = list(dict.fromkeys(line for line in lines if re.sub(r"\s+", "", line) in page_text))
        return items or None

    def _generate(self, prompt: str, system_instruction: str | None = None) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction or self.SUMMARY_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        url = self.API_URL.format(model=self.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                self._wait_for_rate_limit()
            except RuntimeError:
                SiteSummarizer._thread_state.rate_limited = True
                raise
            response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
            if response.status_code == 429 and attempt < self.MAX_RETRIES:
                logger.info(f"Gemini APIのレート制限に達したため {self.RETRY_WAIT_SEC:.0f}秒待って再試行します")
                time.sleep(self.RETRY_WAIT_SEC)
                continue
            if response.status_code == 429:
                with SiteSummarizer._rate_lock:
                    SiteSummarizer._cooldown_until = time.monotonic() + self.QUOTA_COOLDOWN_SEC
                SiteSummarizer._thread_state.rate_limited = True
                logger.warning(f"Gemini APIのレート制限が続くため、{self.QUOTA_COOLDOWN_SEC / 60:.0f}分間サイト概要の要約を停止します(1日の上限到達の可能性があります)")
            response.raise_for_status()
            break

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError(f"応答に候補がありません: {data.get('promptFeedback', '')}")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(str(p.get("text", "")) for p in parts if not p.get("thought"))

    def _wait_for_rate_limit(self) -> None:
        """全スレッド共通で、前回のリクエストからmin_interval秒以上空ける。"""
        with SiteSummarizer._rate_lock:
            now = time.monotonic()
            if now < SiteSummarizer._cooldown_until:
                raise RuntimeError("レート制限のクールダウン中のためスキップしました")
            wait = SiteSummarizer._last_request_at + self.min_interval - now
            # 待機中も枠を予約しておくため、ロック内で次の送信時刻を確定させる
            SiteSummarizer._last_request_at = max(now, SiteSummarizer._last_request_at + self.min_interval)
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _clean_output(text: str) -> str:
        text = re.sub(r"[\r\n]+", "", text.strip())
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip("「」\"' ")
