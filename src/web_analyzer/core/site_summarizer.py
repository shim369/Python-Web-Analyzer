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

    def _generate(self, prompt: str) -> str:
        payload = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "あなたはWebサイトの調査担当者です。与えられたサイト本文から、"
                            "そのサイトを運営する会社・団体の名称と事業内容(何をしている会社か)を、"
                            "日本語で100文字前後の1〜2文に要約してください。"
                            "「〇〇株式会社のコーポレートサイト。〜を手がける。」のような体言止め中心の簡潔な文体にしてください。"
                            "本文から読み取れない事実は書かず、前置き・引用符・改行は付けず要約文だけを出力してください。"
                        )
                    }
                ]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        url = self.API_URL.format(model=self.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        for attempt in range(self.MAX_RETRIES + 1):
            self._wait_for_rate_limit()
            response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
            if response.status_code == 429 and attempt < self.MAX_RETRIES:
                logger.info(f"Gemini APIのレート制限に達したため {self.RETRY_WAIT_SEC:.0f}秒待って再試行します")
                time.sleep(self.RETRY_WAIT_SEC)
                continue
            if response.status_code == 429:
                with SiteSummarizer._rate_lock:
                    SiteSummarizer._cooldown_until = time.monotonic() + self.QUOTA_COOLDOWN_SEC
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
