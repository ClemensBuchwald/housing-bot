"""Die Housing-Bot-eigene LLM-Schicht.

Fachlogik → LLMProvider → AnthropicProvider. Kein Fachmodul kennt ab hier noch
das Anthropic-SDK, und keine SDK-Ausnahme dringt nach draußen.
"""
from pathlib import Path

import pytest

from src.llm import factory
from src.llm.anthropic_provider import _uebersetze, _zu_result
from src.llm.base import LLMProvider, LLMResult
from src.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
    zu_llm_fehler,
)
from tests.fakes import FakeProvider, benutze_provider


# --- Fehlerübersetzung -----------------------------------------------------

def _sdk_fehler(name, status=None, headers=None):
    """Baut eine Attrappe einer SDK-Ausnahme (Klassenname + Statuscode)."""
    typ = type(name, (Exception,), {})
    e = typ("nachgestellt")
    if status is not None:
        e.status_code = status
    if headers is not None:
        e.response = type("R", (), {"headers": headers})()
    return e


@pytest.mark.parametrize("name,status,erwartet", [
    ("AuthenticationError", 401, LLMAuthError),
    ("PermissionDeniedError", 403, LLMAuthError),
    ("RateLimitError", 429, LLMRateLimitError),
    ("APIConnectionError", None, LLMTemporaryError),
    ("APITimeoutError", None, LLMTemporaryError),
    ("InternalServerError", 500, LLMTemporaryError),
    ("APIStatusError", 503, LLMTemporaryError),
    ("BadRequestError", 400, LLMConfigError),
    ("NotFoundError", 404, LLMConfigError),
    ("UnprocessableEntityError", 422, LLMConfigError),
])
def test_sdk_fehler_werden_uebersetzt(name, status, erwartet):
    assert isinstance(_uebersetze(_sdk_fehler(name, status)), erwartet)


def test_unbekannter_fehler_wird_vorsichtig_eingeordnet():
    """Im Zweifel vorübergehend: Der Eintrag bleibt wiederholbar, statt als
    endgueltig gescheitert zu gelten."""
    e = _uebersetze(_sdk_fehler("VoelligNeuerFehler"))
    assert isinstance(e, LLMTemporaryError)
    assert e.retryable is True


def test_retry_after_wird_uebernommen():
    e = _uebersetze(_sdk_fehler("RateLimitError", 429, {"retry-after": "42"}))
    assert isinstance(e, LLMRateLimitError)
    assert e.retry_after == 42.0


def test_ratelimit_ohne_header_bleibt_nutzbar():
    e = _uebersetze(_sdk_fehler("RateLimitError", 429))
    assert isinstance(e, LLMRateLimitError)
    assert e.retry_after is None


def test_timeout_und_verbindungsfehler_der_standardbibliothek():
    assert isinstance(_uebersetze(TimeoutError("weg")), LLMTemporaryError)
    assert isinstance(_uebersetze(ConnectionError("weg")), LLMTemporaryError)


def test_zu_llm_fehler_laesst_bekannte_fehler_durch():
    original = LLMAuthError("schon eingeordnet")
    assert zu_llm_fehler(original) is original


def test_zu_llm_fehler_faengt_programmierfehler():
    e = zu_llm_fehler(AttributeError("Tippfehler"))
    assert isinstance(e, LLMTemporaryError)
    assert "AttributeError" in str(e)


# --- Fehlereigenschaften ---------------------------------------------------

def test_auth_fehler_verbraucht_nichts_und_wartet_nicht():
    assert LLMAuthError.retryable is False
    assert LLMAuthError.verbraucht_versuch is False
    assert LLMAuthError.max_versuche == 0


def test_konfigurationsfehler_ist_nicht_wiederholbar():
    assert LLMConfigError.retryable is False
    assert LLMConfigError.verbraucht_versuch is False


def test_alle_fehler_sind_llm_fehler():
    for k in (LLMAuthError, LLMRateLimitError, LLMTemporaryError,
              LLMProtocolError, LLMConfigError):
        assert issubclass(k, LLMError)
        assert k.kategorie != "unbekannt", f"{k.__name__} braucht eine eigene Kategorie"


# --- Antwortnormalisierung -------------------------------------------------

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_antwort_wird_normalisiert():
    resp = _Block(
        content=[_Block(type="text", text="Hallo "), _Block(type="text", text="Welt")],
        stop_reason="end_turn",
    )
    r = _zu_result(resp)
    assert r.text == "Hallo Welt"
    assert r.stop_reason == "end_turn"
    assert r.tool_calls == []


def test_werkzeugaufruf_wird_normalisiert():
    resp = _Block(
        content=[_Block(type="tool_use", id="t1", name="suche", input={"a": 1})],
        stop_reason="tool_use",
    )
    r = _zu_result(resp)
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "suche"
    assert r.tool_calls[0].input == {"a": 1}
    # _zu_result kopiert die Liste, die Bloecke selbst bleiben dieselben Objekte —
    # nur so kann die Tool-Schleife sie unveraendert zurueckreichen.
    assert list(r.raw_content) == list(resp.content)
    assert r.raw_content[0] is resp.content[0], "Rohbloecke duerfen nicht umgebaut werden"


def test_stop_sequence_gilt_als_regulaeres_ende():
    r = _zu_result(_Block(content=[_Block(type="text", text="x")], stop_reason="stop_sequence"))
    assert r.stop_reason == "end_turn"


def test_unbekannter_abbruchgrund_wird_nicht_verschluckt():
    r = _zu_result(_Block(content=[_Block(type="text", text="x")], stop_reason="etwas_neues"))
    assert r.stop_reason == "other"


def test_max_tokens_bleibt_erkennbar():
    r = _zu_result(_Block(content=[_Block(type="text", text='{"passt": tr')],
                          stop_reason="max_tokens"))
    assert r.stop_reason == "max_tokens"


def test_kaputte_antwort_ist_protokollfehler():
    class Kaputt:
        @property
        def content(self):
            raise ValueError("kein content")
    with pytest.raises(LLMProtocolError):
        _zu_result(Kaputt())


# --- Fabrik ----------------------------------------------------------------

def test_fabrik_lehnt_unbekannten_anbieter_ab(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "erfundener-anbieter")
    factory.set_provider(None)
    with pytest.raises(LLMConfigError):
        factory.get_provider(force_new=True)
    factory.set_provider(None)


def test_fehlender_schluessel_ist_konfigurationsfehler(monkeypatch):
    from src.llm.anthropic_provider import AnthropicProvider
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        AnthropicProvider()


def test_modell_ist_zentral_konfigurierbar(monkeypatch):
    from src.llm.anthropic_provider import AnthropicProvider, DEFAULT_MODEL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-nicht-echt")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert AnthropicProvider().model == DEFAULT_MODEL
    monkeypatch.setenv("LLM_MODEL", "claude-anderes-modell")
    assert AnthropicProvider().model == "claude-anderes-modell"


def test_fake_erfuellt_das_protokoll():
    assert isinstance(FakeProvider(), LLMProvider)


def test_set_provider_stellt_wieder_her():
    with benutze_provider(FakeProvider(["x"])) as p:
        assert factory.get_provider() is p
    assert factory._provider is None


# --- Abgrenzung ------------------------------------------------------------

def test_fachlogik_kennt_das_sdk_nicht_mehr():
    for datei in ("src/evaluator.py", "src/agent.py", "src/main.py"):
        quelltext = Path(datei).read_text()
        assert "import anthropic" not in quelltext, f"{datei} greift noch direkt aufs SDK zu"
        assert "anthropic.Anthropic" not in quelltext


def test_nur_der_provider_kennt_das_sdk():
    import subprocess
    treffer = subprocess.run(
        ["grep", "-rl", "import anthropic", "src/"],
        capture_output=True, text=True,
    ).stdout.split()
    assert treffer == ["src/llm/anthropic_provider.py"], \
        f"SDK-Zugriff ausserhalb der Provider-Kapsel: {treffer}"


def test_llm_schicht_ist_eigenstaendig():
    """Die Schicht gehoert ausschliesslich zum Housing Bot: keine Verbindung zu
    einem anderen Projekt, kein gemeinsamer Runner, keine Weiterleitung."""
    import re
    muster = re.compile(r"\bdcc\b", re.IGNORECASE)
    for datei in sorted(Path("src/llm").glob("*.py")):
        zeilen = datei.read_text().splitlines()
        treffer = [z for z in zeilen if muster.search(z) and not z.strip().startswith("#")]
        assert treffer == [], f"{datei} verweist auf ein Fremdprojekt: {treffer}"
    # Importiert werden darf nur Projekteigenes und die Standardbibliothek.
    fremdimporte = []
    for datei in sorted(Path("src/llm").glob("*.py")):
        for z in datei.read_text().splitlines():
            m = re.match(r"\s*(?:from|import)\s+([\w.]+)", z)
            if m and m.group(1).split(".")[0] not in (
                    "src", "anthropic", "__future__", "logging", "os", "time",
                    "threading", "typing", "dataclasses", "json", "typing_extensions"):
                fremdimporte.append((datei.name, m.group(1)))
    assert fremdimporte == [], f"unerwartete Abhaengigkeit: {fremdimporte}"
