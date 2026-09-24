"""Núcleo compartilhado pelos tradutores: acesso ao serviço de tradução e utilidades."""

import json
import os
import re
import time
import urllib.parse
import urllib.request

MAX_CHARS = 4500  # limite seguro por requisição (Google aceita ~5000)
SEP = "\n\n"

IDIOMAS = {
    "Detectar automaticamente": "auto",
    "Português": "pt",
    "Inglês": "en",
    "Espanhol": "es",
    "Francês": "fr",
    "Alemão": "de",
    "Italiano": "it",
    "Japonês": "ja",
    "Chinês (simplificado)": "zh-CN",
    "Russo": "ru",
    "Coreano": "ko",
}


def gravar_json(caminho, dados):
    """Grava de forma atômica: um travamento no meio nunca deixa o arquivo corrompido."""
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)


def ler_json(caminho):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def quebrar_longo(texto, limite=MAX_CHARS):
    """Divide um parágrafo muito longo em pedaços por frases."""
    if len(texto) <= limite:
        return [texto]
    frases = re.split(r"(?<=[.!?…])\s+", texto)
    pedacos, atual = [], ""
    for frase in frases:
        while len(frase) > limite:
            pedacos.append(frase[:limite])
            frase = frase[limite:]
        if len(atual) + len(frase) + 1 > limite:
            pedacos.append(atual)
            atual = frase
        else:
            atual = f"{atual} {frase}".strip()
    if atual:
        pedacos.append(atual)
    return pedacos


ENDPOINTS = (
    ("https://clients5.google.com/translate_a/t", "t"),
    ("https://translate.googleapis.com/translate_a/single", "single"),
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}


def _requisitar(url, formato, texto, origem, destino):
    params = {"client": "gtx", "sl": origem, "tl": destino}
    if formato == "single":
        params.update(dt="t", dj="1")
    corpo = urllib.parse.urlencode({"q": texto}).encode("utf-8")
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", data=corpo, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        dados = json.loads(r.read().decode("utf-8"))
    if formato == "single":
        return "".join(s.get("trans", "") for s in dados.get("sentences", []))
    item = dados[0]
    return item[0] if isinstance(item, list) else item


class Tradutor:
    def __init__(self, origem, destino, tentativas=4):
        self.origem, self.destino = origem, destino
        self.tentativas = tentativas

    def _traduzir(self, texto):
        espera = 2
        ultimo_erro = None
        for i in range(self.tentativas):
            for url, formato in ENDPOINTS:
                try:
                    r = _requisitar(url, formato, texto, self.origem, self.destino)
                    if r:
                        return r.replace("​", "")  # o Google às vezes insere espaços invisíveis
                except Exception as e:
                    ultimo_erro = e
            if i < self.tentativas - 1:
                time.sleep(espera)
                espera *= 2
        raise RuntimeError(f"Falha ao contatar o serviço de tradução: {ultimo_erro}")

    def paragrafo(self, texto):
        return " ".join(self._traduzir(p) for p in quebrar_longo(texto))

    def lote(self, paragrafos):
        """Traduz vários parágrafos numa única requisição; se a divisão falhar, um por um."""
        if len(paragrafos) == 1:
            return [self.paragrafo(paragrafos[0])]
        resultado = self._traduzir(SEP.join(paragrafos))
        partes = [p.strip() for p in re.split(r"\n\s*\n", resultado) if p.strip()]
        if len(partes) == len(paragrafos):
            return partes
        return [self.paragrafo(p) for p in paragrafos]


def montar_lotes(paragrafos):
    lotes, atual, tam = [], [], 0
    for p in paragrafos:
        if atual and tam + len(p) + len(SEP) > MAX_CHARS:
            lotes.append(atual)
            atual, tam = [], 0
        atual.append(p)
        tam += len(p) + len(SEP)
    if atual:
        lotes.append(atual)
    return lotes
