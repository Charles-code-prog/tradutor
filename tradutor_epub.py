"""Tradutor de livros EPUB com limite de parágrafos definido na interface.

Cada parágrafo é traduzido com a formatação interna (itálico, negrito, links, marcadores de
página) representada por tags numeradas (<g1>...</g1>, <x2/>), que o tradutor preserva.
As traduções ficam num arquivo de progresso (.progresso.jsonl) gravado a cada bloco, o que
permite retomar de onde parou se o programa travar ou for fechado.
"""

import copy
import hashlib
import json
import os
import posixpath
import queue
import re
import sys
import threading
import tkinter as tk
import urllib.parse
import zipfile
from tkinter import filedialog, messagebox, ttk

from lxml import etree

from nucleo import IDIOMAS, Tradutor, gravar_json, ler_json, montar_lotes

PASTA_CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TradutorEPUB")
ARQ_ULTIMO = os.path.join(PASTA_CONFIG, "ultimo_trabalho.json")

BLOCOS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "blockquote", "td", "th",
    "caption", "figcaption", "div", "section", "article", "aside", "header", "footer", "nav",
    "ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr", "figure", "body", "main",
    "hgroup", "address", "summary", "details",
}
IGNORAR = {"pre", "script", "style", "code", "svg", "math"}
TEM_LETRA = re.compile(r"[^\W\d_]")
TAG_MARCADOR = re.compile(r"<(/?)([gx])(\d+)(/?)>")


def nome_local(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _espacos(texto):
    return re.sub(r"\s+", " ", texto or "")


# ---------- estrutura do EPUB ----------

class Livro:
    """Lê o EPUB e extrai as unidades de tradução (parágrafos) na ordem de leitura."""

    def __init__(self, caminho):
        self.caminho = caminho
        with zipfile.ZipFile(caminho) as z:
            self.nomes = z.namelist()
            self.arquivos = {n: z.read(n) for n in self.nomes}
        self.opf = self._achar_opf()
        self.documentos, self.ncx = self._ler_spine()
        self.arvores = {}
        self.unidades = []  # (documento, elemento, texto_marcado, mapa, capitular)
        for nome in self.documentos + ([self.ncx] if self.ncx else []):
            self._extrair(nome)

    def _achar_opf(self):
        container = etree.fromstring(self.arquivos["META-INF/container.xml"])
        rootfile = container.find(".//{*}rootfile")
        return rootfile.get("full-path")

    def _ler_spine(self):
        opf = etree.fromstring(self.arquivos[self.opf])
        base = posixpath.dirname(self.opf)
        manifesto = {}
        ncx = None
        for item in opf.iterfind(".//{*}manifest/{*}item"):
            href = posixpath.normpath(posixpath.join(base, urllib.parse.unquote(item.get("href", ""))))
            manifesto[item.get("id")] = (href, item.get("media-type", ""))
            if item.get("media-type") == "application/x-dtbncx+xml":
                ncx = href
        docs = []
        for ref in opf.iterfind(".//{*}spine/{*}itemref"):
            href, tipo = manifesto.get(ref.get("idref"), (None, ""))
            if href in self.arquivos and "html" in tipo and href not in docs:
                docs.append(href)
        return docs, (ncx if ncx in self.arquivos else None)

    def _extrair(self, nome):
        parser = etree.XMLParser(recover=True, resolve_entities=False, remove_blank_text=False)
        try:
            arvore = etree.ElementTree(etree.fromstring(self.arquivos[nome], parser))
        except etree.XMLSyntaxError:
            return
        if arvore.getroot() is None:
            return
        self.arvores[nome] = arvore
        raiz = arvore.getroot()
        if nome == self.ncx:
            for el in raiz.iter("{*}text"):
                if TEM_LETRA.search(el.text or ""):
                    self.unidades.append((nome, el, _espacos(el.text).strip(), {}, None))
            return
        corpo = raiz.find("{*}body")
        if corpo is not None:
            self._percorrer(nome, corpo)

    def _percorrer(self, nome, el):
        tag = nome_local(el)
        if tag in IGNORAR:
            return
        if tag in BLOCOS and not any(nome_local(d) in BLOCOS for d in el.iterdescendants()):
            if TEM_LETRA.search("".join(el.itertext())):
                capitular = _separar_capitular(el)
                mapa = {}
                marcado = _espacos(_marcar(el, mapa)).strip()
                self.unidades.append((nome, el, marcado, mapa, capitular))
            return
        for filho in el:
            if isinstance(filho.tag, str):
                self._percorrer(nome, filho)

    def assinatura(self):
        return hashlib.sha1("\x00".join(u[2] for u in self.unidades).encode("utf-8")).hexdigest()

    def salvar(self, destino_arquivo, traducoes, idioma):
        alterados = set()
        for i, (nome, el, _, mapa, capitular) in enumerate(self.unidades):
            if i in traducoes:
                _aplicar(el, traducoes[i], mapa, capitular)
                alterados.add(nome)
        conteudo = dict(self.arquivos)
        for nome in alterados:
            raiz = self.arvores[nome].getroot()
            if nome != self.ncx:
                raiz.set("lang", idioma)
                raiz.set("{http://www.w3.org/XML/1998/namespace}lang", idioma)
            conteudo[nome] = etree.tostring(self.arvores[nome], xml_declaration=True, encoding="utf-8")
        if alterados:
            conteudo[self.opf] = self._opf_com_idioma(idioma)

        tmp = destino_arquivo + ".tmp"
        with zipfile.ZipFile(tmp, "w") as z:
            if "mimetype" in conteudo:
                z.writestr(zipfile.ZipInfo("mimetype"), conteudo["mimetype"], compress_type=zipfile.ZIP_STORED)
            for nome in self.nomes:
                if nome != "mimetype":
                    z.writestr(nome, conteudo[nome], compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp, destino_arquivo)

    def _opf_com_idioma(self, idioma):
        opf = etree.ElementTree(etree.fromstring(self.arquivos[self.opf]))
        for lang in opf.getroot().iterfind(".//{http://purl.org/dc/elements/1.1/}language"):
            lang.text = idioma
        return etree.tostring(opf, xml_declaration=True, encoding="utf-8")


def _separar_capitular(bloco):
    """Remove a letra capitular (ex.: <span class="dropcap">L</span>ater) antes de traduzir,
    guardando um molde para reaplicá-la na primeira letra do texto traduzido."""
    if (bloco.text or "").strip():
        return None
    for filho in bloco:
        texto = "".join(filho.itertext()) if isinstance(filho.tag, str) else ""
        if not texto.strip():
            if (filho.tail or "").strip():
                return None
            continue
        cauda = filho.tail or ""
        if len(texto.strip()) <= 2 and TEM_LETRA.search(texto) and cauda[:1].isalpha():
            molde = copy.deepcopy(filho)
            molde.tail = None
            anterior = filho.getprevious()
            if anterior is not None:
                anterior.tail = (anterior.tail or "") + texto + cauda
            else:
                bloco.text = (bloco.text or "") + texto + cauda
            bloco.remove(filho)
            return molde
        return None
    return None


def _marcar(el, mapa):
    """Converte o conteúdo do elemento em texto com marcadores <gN>...</gN> e <xN/>."""
    partes = [el.text or ""]
    for filho in el:
        n = len(mapa) + 1
        vazio = not isinstance(filho.tag, str) or (len(filho) == 0 and not (filho.text or "").strip())
        if vazio:
            mapa[n] = ("x", filho)
            partes.append(f"<x{n}/>")
        else:
            mapa[n] = ("g", filho)
            partes.append(f"<g{n}>" + _marcar(filho, mapa) + f"</g{n}>")
        partes.append(filho.tail or "")
    return "".join(partes)


def normalizar_marcadores(texto):
    return re.sub(r"<\s*(/?)\s*([gx])\s*(\d+)\s*(/?)\s*>", r"<\1\2\3\4>", texto)


def _analisar(texto, mapa):
    """Transforma o texto traduzido numa árvore (elemento, [filhos]); None se os marcadores vieram quebrados."""
    raiz = (None, [])
    pilha = [raiz]
    usados = set()
    pos = 0
    for m in TAG_MARCADOR.finditer(texto):
        if m.start() > pos:
            pilha[-1][1].append(texto[pos:m.start()])
        pos = m.end()
        fecha, tipo, n, auto = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if n not in mapa or mapa[n][0] != tipo:
            return None
        if tipo == "x":
            if fecha or n in usados:
                return None
            usados.add(n)
            pilha[-1][1].append((mapa[n][1], []))
        elif fecha:
            if len(pilha) < 2 or pilha[-1][0] is not mapa[n][1]:
                return None
            pilha.pop()
        else:
            if auto or n in usados:
                return None
            usados.add(n)
            no = (mapa[n][1], [])
            pilha[-1][1].append(no)
            pilha.append(no)
    if len(pilha) != 1:
        return None
    if pos < len(texto):
        raiz[1].append(texto[pos:])
    # elementos vazios (âncoras, marcadores de página, imagens) nunca podem sumir
    faltando = [(el, []) for n, (tipo, el) in mapa.items() if tipo == "x" and n not in usados]
    raiz[1][:0] = faltando
    return raiz


def _preencher(el, filhos, preservar_x):
    el.text = None
    for filho in list(el):
        el.remove(filho)
    ultimo = None
    for item in filhos:
        if isinstance(item, str):
            if ultimo is None:
                el.text = (el.text or "") + item
            else:
                ultimo.tail = (ultimo.tail or "") + item
        else:
            sub, netos = item
            sub.tail = None
            if not preservar_x(sub):
                _preencher(sub, netos, preservar_x)
            el.append(sub)
            ultimo = sub


def _aplicar(bloco, traducao, mapa, capitular):
    elementos_x = {id(el) for tipo, el in mapa.values() if tipo == "x"}
    arvore = _analisar(normalizar_marcadores(traducao), mapa)
    if arvore is None:  # marcadores corrompidos: mantém só o texto, preservando os elementos vazios
        texto = TAG_MARCADOR.sub("", normalizar_marcadores(traducao))
        arvore = (None, [(el, []) for tipo, el in mapa.values() if tipo == "x"] + [texto])
    _preencher(bloco, arvore[1], lambda el: id(el) in elementos_x)
    if capitular is not None:
        _reaplicar_capitular(bloco, capitular)


def _reaplicar_capitular(bloco, molde):
    def aplicar(texto):
        texto = texto.lstrip()
        m = TEM_LETRA.search(texto)
        if not m:
            return None
        cap = copy.deepcopy(molde)
        interno = cap
        while len(interno):
            interno = interno[-1]
        interno.text = texto[:m.end()]
        cap.tail = texto[m.end():]
        return cap

    if (bloco.text or "").strip():
        cap = aplicar(bloco.text)
        if cap is not None:
            bloco.text = None
            bloco.insert(0, cap)
        return
    for i, filho in enumerate(bloco):
        if (filho.tail or "").strip():
            cap = aplicar(filho.tail)
            if cap is not None:
                filho.tail = None
                bloco.insert(i + 1, cap)
            return
        if "".join(filho.itertext()).strip():
            return


# ---------- progresso (JSON Lines: cabeçalho + uma tradução por linha) ----------

def arquivo_progresso(saida):
    return saida + ".progresso.jsonl"


def ler_progresso(saida):
    """Retorna (cabeçalho, {índice: tradução}); linhas incompletas de um travamento são ignoradas."""
    try:
        with open(arquivo_progresso(saida), encoding="utf-8") as f:
            linhas = f.read().split("\n")
    except OSError:
        return None, {}
    cabecalho, traducoes = None, {}
    for linha in linhas:
        try:
            dado = json.loads(linha)
        except ValueError:
            continue
        if cabecalho is None:
            cabecalho = dado
        elif "i" in dado and "t" in dado:
            traducoes[dado["i"]] = dado["t"]
    return cabecalho, traducoes


def iniciar_progresso(saida, cabecalho):
    with open(arquivo_progresso(saida), "w", encoding="utf-8") as f:
        f.write(json.dumps(cabecalho, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    gravar_json(ARQ_ULTIMO, {"saida": saida})


def ultimo_trabalho():
    info = ler_json(ARQ_ULTIMO)
    return info.get("saida") if info else None


def apagar_progresso(saida):
    alvos = [arquivo_progresso(saida)]
    if ultimo_trabalho() == saida:
        alvos.append(ARQ_ULTIMO)
    for caminho in alvos:
        try:
            os.remove(caminho)
        except OSError:
            pass


# ---------- interface ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tradutor de EPUB")
        self.geometry("760x600")
        self.minsize(640, 520)

        self.livro = None
        self.assinatura = None
        self.traduzidos = set()
        self.fila = queue.Queue()
        self.cancelar = threading.Event()
        self.trabalhando = False

        self.var_entrada = tk.StringVar()
        self.var_saida = tk.StringVar()
        self.var_origem = tk.StringVar(value="Detectar automaticamente")
        self.var_destino = tk.StringVar(value="Português")
        self.var_inicio = tk.IntVar(value=1)
        self.var_qtd = tk.IntVar(value=100)
        self.var_todos = tk.BooleanVar(value=False)
        self.var_info = tk.StringVar(value="Nenhum livro carregado.")
        self.var_previa = tk.StringVar()

        self._montar_ui()
        self.var_inicio.trace_add("write", lambda *_: self._atualizar_previa())
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)
        self.after(100, self._processar_fila)
        self.after(300, self._oferecer_ultimo_trabalho)

    def _montar_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Livro EPUB:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.var_entrada).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Abrir...", command=self._escolher_entrada).grid(row=0, column=2, **pad)

        ttk.Label(frm, text="Salvar em:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.var_saida).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Escolher...", command=self._escolher_saida).grid(row=1, column=2, **pad)

        ttk.Label(frm, textvariable=self.var_info, foreground="#555").grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)

        idi = ttk.LabelFrame(frm, text="Idiomas", padding=8)
        idi.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(idi, text="De:").pack(side="left")
        ttk.Combobox(idi, textvariable=self.var_origem, values=list(IDIOMAS),
                     state="readonly", width=24).pack(side="left", padx=6)
        ttk.Label(idi, text="Para:").pack(side="left", padx=(16, 0))
        ttk.Combobox(idi, textvariable=self.var_destino,
                     values=[k for k in IDIOMAS if IDIOMAS[k] != "auto"],
                     state="readonly", width=24).pack(side="left", padx=6)

        lim = ttk.LabelFrame(frm, text="Limite de tradução (parágrafos)", padding=8)
        lim.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        lim.columnconfigure(5, weight=1)
        ttk.Label(lim, text="Começar no parágrafo:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(lim, from_=1, to=10_000_000, textvariable=self.var_inicio, width=10).grid(
            row=0, column=1, padx=6, sticky="w")
        ttk.Label(lim, text="Quantidade de parágrafos:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.spin_qtd = ttk.Spinbox(lim, from_=1, to=10_000_000, textvariable=self.var_qtd, width=10)
        self.spin_qtd.grid(row=0, column=3, padx=6, sticky="w")
        ttk.Checkbutton(lim, text="Traduzir até o fim", variable=self.var_todos,
                        command=self._alternar_todos).grid(row=0, column=4, padx=(16, 0))
        ttk.Label(lim, textvariable=self.var_previa, foreground="#555", wraplength=680).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        botoes = ttk.Frame(frm)
        botoes.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_traduzir = ttk.Button(botoes, text="Traduzir", command=self._iniciar)
        self.btn_traduzir.pack(side="left")
        self.btn_cancelar = ttk.Button(botoes, text="Cancelar", command=self.cancelar.set, state="disabled")
        self.btn_cancelar.pack(side="left", padx=8)
        self.btn_gerar = ttk.Button(botoes, text="Gerar EPUB agora", command=self._gerar_agora)
        self.btn_gerar.pack(side="left")
        self.progresso = ttk.Progressbar(botoes, mode="determinate")
        self.progresso.pack(side="left", fill="x", expand=True, padx=8)

        self.log = tk.Text(frm, height=12, wrap="word", state="disabled")
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)

    def _alternar_todos(self):
        self.spin_qtd.configure(state="disabled" if self.var_todos.get() else "normal")

    def _registrar(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _ocupado(self, sim):
        self.trabalhando = sim
        estado = "disabled" if sim else "normal"
        self.btn_traduzir.configure(state=estado)
        self.btn_gerar.configure(state=estado)
        self.btn_cancelar.configure(state="normal" if sim else "disabled")

    def _atualizar_previa(self):
        if not self.livro:
            self.var_previa.set("")
            return
        try:
            i = int(self.var_inicio.get())
        except (tk.TclError, ValueError):
            return
        if not 1 <= i <= len(self.livro.unidades):
            self.var_previa.set("")
            return
        nome, _, texto, _, _ = self.livro.unidades[i - 1]
        texto = TAG_MARCADOR.sub("", texto)
        texto = texto if len(texto) <= 110 else texto[:110] + "…"
        self.var_previa.set(f"Parágrafo {i} ({posixpath.basename(nome)}): “{texto}”")

    def _atualizar_info(self):
        total = len(self.livro.unidades)
        feitos = len(self.traduzidos)
        self.var_info.set(f"{total} parágrafos em {len(self.livro.documentos)} seções · "
                          f"{feitos} já traduzidos ({feitos * 100 // max(total, 1)}%)")

    def _primeiro_pendente(self):
        for i in range(len(self.livro.unidades)):
            if i not in self.traduzidos:
                return i + 1
        return len(self.livro.unidades) + 1

    # ---------- arquivos ----------
    def _escolher_entrada(self):
        caminho = filedialog.askopenfilename(filetypes=[("Livros EPUB", "*.epub"), ("Todos", "*.*")])
        if not caminho:
            return
        self.var_entrada.set(caminho)
        base, _ = os.path.splitext(caminho)
        self.var_saida.set(f"{base}_traduzido.epub")
        if self._carregar(caminho):
            self._verificar_retomada(perguntar=True)

    def _escolher_saida(self):
        caminho = filedialog.asksaveasfilename(defaultextension=".epub",
                                               filetypes=[("Livros EPUB", "*.epub")])
        if caminho:
            self.var_saida.set(caminho)
            if self.livro:
                self._verificar_retomada(perguntar=True)

    def _carregar(self, caminho):
        try:
            self.livro = Livro(caminho)
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao ler o EPUB:\n{e}")
            self.livro = None
            return False
        self.assinatura = self.livro.assinatura()
        self.traduzidos = set()
        self._atualizar_info()
        self.var_inicio.set(1)
        self._atualizar_previa()
        self._registrar(f"Carregado: {os.path.basename(caminho)} ({len(self.livro.unidades)} parágrafos)")
        return True

    def _progresso_valido(self, saida):
        cabecalho, traducoes = ler_progresso(saida)
        if not cabecalho or cabecalho.get("assinatura") != self.assinatura:
            return None, {}
        return cabecalho, traducoes

    def _verificar_retomada(self, perguntar):
        cabecalho, traducoes = self._progresso_valido(self.var_saida.get().strip())
        if not traducoes:
            return
        if perguntar and not messagebox.askyesno(
                "Tradução em andamento",
                f"Já existem {len(traducoes)} de {len(self.livro.unidades)} parágrafos traduzidos "
                "deste livro.\n\nContinuar de onde parou?"):
            return
        self._aplicar_progresso(cabecalho, traducoes)

    def _aplicar_progresso(self, cabecalho, traducoes):
        self.traduzidos = set(traducoes)
        if cabecalho.get("origem") in IDIOMAS:
            self.var_origem.set(cabecalho["origem"])
        if cabecalho.get("destino") in IDIOMAS:
            self.var_destino.set(cabecalho["destino"])
        self._atualizar_info()
        self.var_inicio.set(min(self._primeiro_pendente(), len(self.livro.unidades)))
        self._registrar(f"Retomando: {len(traducoes)} parágrafos já traduzidos; "
                        f"próximo pendente é o {self.var_inicio.get()}.")

    def _oferecer_ultimo_trabalho(self):
        saida = ultimo_trabalho()
        if not saida:
            return
        cabecalho, traducoes = ler_progresso(saida)
        if not cabecalho or not os.path.isfile(cabecalho.get("entrada", "")):
            return
        if not messagebox.askyesno(
                "Tradução interrompida",
                f"A última tradução não terminou:\n\n{os.path.basename(cabecalho['entrada'])}\n"
                f"{len(traducoes)} de {cabecalho.get('total', '?')} parágrafos traduzidos.\n\n"
                "Continuar de onde parou?"):
            return
        self.var_entrada.set(cabecalho["entrada"])
        self.var_saida.set(saida)
        if not self._carregar(cabecalho["entrada"]):
            return
        cabecalho, traducoes = self._progresso_valido(saida)
        if cabecalho:
            self._aplicar_progresso(cabecalho, traducoes)
        else:
            messagebox.showwarning("Atenção", "O livro original foi alterado desde a última tradução; "
                                              "não é possível retomar automaticamente.")

    def _ao_fechar(self):
        if self.trabalhando and not messagebox.askyesno(
                "Sair", "Há uma tradução em andamento. O que já foi traduzido fica salvo e "
                        "poderá ser retomado depois.\n\nSair mesmo assim?"):
            return
        self.destroy()

    # ---------- tradução ----------
    def _validar_arquivos(self):
        entrada, saida = self.var_entrada.get().strip(), self.var_saida.get().strip()
        if not entrada or not os.path.isfile(entrada):
            messagebox.showwarning("Atenção", "Escolha um arquivo EPUB válido.")
            return None
        if not saida:
            messagebox.showwarning("Atenção", "Informe onde salvar o livro traduzido.")
            return None
        if os.path.abspath(entrada) == os.path.abspath(saida):
            messagebox.showwarning("Atenção", "O arquivo de saída não pode ser o próprio livro original.")
            return None
        if not self.livro and not self._carregar(entrada):
            return None
        return entrada, saida

    def _iniciar(self):
        if self.trabalhando:
            return
        arquivos = self._validar_arquivos()
        if not arquivos:
            return
        entrada, saida = arquivos
        total = len(self.livro.unidades)
        try:
            inicio = int(self.var_inicio.get())
            qtd = None if self.var_todos.get() else int(self.var_qtd.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Atenção", "Valores de parágrafo inválidos.")
            return
        if not 1 <= inicio <= total:
            messagebox.showwarning("Atenção", f"O parágrafo inicial deve estar entre 1 e {total}.")
            return
        if qtd is not None and qtd < 1:
            messagebox.showwarning("Atenção", "A quantidade deve ser pelo menos 1.")
            return

        cabecalho, traducoes = self._progresso_valido(saida)
        destino_nome = self.var_destino.get()
        if cabecalho and cabecalho.get("destino") != destino_nome and traducoes:
            if not messagebox.askyesno(
                    "Idioma diferente",
                    f"As traduções anteriores deste livro são para {cabecalho.get('destino')}.\n"
                    f"Descartá-las e recomeçar em {destino_nome}?"):
                return
            cabecalho, traducoes = None, {}
        if not cabecalho:
            iniciar_progresso(saida, {
                "entrada": os.path.abspath(entrada), "assinatura": self.assinatura, "total": total,
                "origem": self.var_origem.get(), "destino": destino_nome,
            })
            traducoes = {}
        else:
            gravar_json(ARQ_ULTIMO, {"saida": saida})
        self.traduzidos = set(traducoes)

        fim = total if qtd is None else min(total, inicio - 1 + qtd)
        indices = list(range(inicio - 1, fim))
        self._ocupado(True)
        self.cancelar.clear()
        self.progresso.configure(maximum=len(indices), value=0)
        self._registrar(f"Traduzindo parágrafos {inicio} a {fim} ({len(indices)})...")

        threading.Thread(target=self._trabalho,
                         args=(indices, entrada, saida, IDIOMAS[self.var_origem.get()], IDIOMAS[destino_nome]),
                         daemon=True).start()

    def _trabalho(self, indices, entrada, saida, origem, destino):
        feitos = 0
        erro = None
        try:
            tradutor = Tradutor(origem, destino)
            textos = [self.livro.unidades[i][2] for i in indices]
            posicao = 0
            with open(arquivo_progresso(saida), "a", encoding="utf-8") as f:
                for lote in montar_lotes(textos):
                    if self.cancelar.is_set():
                        break
                    traducoes = tradutor.lote(lote)
                    for texto in traducoes:
                        f.write(json.dumps({"i": indices[posicao], "t": texto}, ensure_ascii=False) + "\n")
                        posicao += 1
                    f.flush()
                    os.fsync(f.fileno())
                    feitos = posicao
                    self.fila.put(("progresso", (feitos, indices[:feitos])))
        except Exception as e:
            erro = str(e)
        self._gerar_epub(entrada, saida, destino)
        self.fila.put(("fim", (feitos, saida, self.cancelar.is_set(), erro)))

    def _gerar_epub(self, entrada, saida, destino):
        """Monta o EPUB de saída: parágrafos traduzidos + o restante no idioma original."""
        try:
            _, traducoes = self._progresso_valido(saida)
            Livro(entrada).salvar(saida, traducoes, destino)
            completo = len(traducoes) >= len(self.livro.unidades)
            if completo:
                apagar_progresso(saida)
            self.fila.put(("epub", (saida, len(traducoes), completo)))
        except Exception as e:
            self.fila.put(("erro_epub", str(e)))

    def _gerar_agora(self):
        if self.trabalhando:
            return
        arquivos = self._validar_arquivos()
        if not arquivos:
            return
        entrada, saida = arquivos
        _, traducoes = self._progresso_valido(saida)
        if not traducoes:
            messagebox.showinfo("Gerar EPUB", "Ainda não há parágrafos traduzidos deste livro.")
            return
        self._ocupado(True)
        destino = IDIOMAS[self.var_destino.get()]

        def tarefa():
            self._gerar_epub(entrada, saida, destino)
            self.fila.put(("livre", None))

        threading.Thread(target=tarefa, daemon=True).start()

    def _processar_fila(self):
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "progresso":
                    feitos, novos = dado
                    self.progresso.configure(value=feitos)
                    self.traduzidos.update(novos)
                    self._atualizar_info()
                elif tipo == "epub":
                    saida, qtd, completo = dado
                    extra = " (livro completo!)" if completo else ""
                    self._registrar(f"EPUB gerado com {qtd} parágrafos traduzidos{extra} → {saida}")
                elif tipo == "erro_epub":
                    self._registrar(f"ERRO ao gerar o EPUB: {dado}")
                    messagebox.showerror("Erro ao gerar EPUB", dado)
                elif tipo == "livre":
                    self._ocupado(False)
                elif tipo == "fim":
                    feitos, saida, cancelado, erro = dado
                    self._ocupado(False)
                    if erro:
                        self._registrar(f"ERRO após {feitos} parágrafos: {erro}")
                        messagebox.showerror("Erro na tradução",
                                             f"{erro}\n\nO que já foi traduzido está salvo; "
                                             "clique em Traduzir para continuar.")
                    else:
                        self._registrar(f"{'Cancelado' if cancelado else 'Concluído'}: "
                                        f"{feitos} parágrafos traduzidos nesta rodada.")
                    proximo = self._primeiro_pendente()
                    if proximo <= len(self.livro.unidades):
                        self.var_inicio.set(proximo)
                        self._registrar(f"Próxima tradução começará no parágrafo {proximo}.")
                    else:
                        self._registrar("Livro inteiro traduzido.")
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()
