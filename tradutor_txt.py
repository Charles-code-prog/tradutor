"""Tradutor de arquivos TXT com limite de parágrafos definido na interface."""

import hashlib
import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from nucleo import IDIOMAS, SEP, Tradutor, montar_lotes
from nucleo import gravar_json as _gravar_json, ler_json as _ler_json

PASTA_CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TradutorTXT")
ARQ_ULTIMO = os.path.join(PASTA_CONFIG, "ultimo_trabalho.json")


def arquivo_progresso(saida):
    return saida + ".progresso.json"


def salvar_progresso(saida, dados):
    _gravar_json(arquivo_progresso(saida), dados)
    _gravar_json(ARQ_ULTIMO, {"saida": saida})


def ler_progresso(saida):
    return _ler_json(arquivo_progresso(saida))


def ultimo_trabalho():
    info = _ler_json(ARQ_ULTIMO)
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


def assinatura(paragrafos):
    return hashlib.sha1("\x00".join(paragrafos).encode("utf-8")).hexdigest()


def ler_texto(caminho):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(caminho, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError("Não foi possível decodificar o arquivo.")


def dividir_paragrafos(texto):
    """Parágrafos separados por linha em branco; se não houver, cada linha é um parágrafo."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"\n\s*\n", texto):
        partes = re.split(r"\n\s*\n", texto)
    else:
        partes = texto.split("\n")
    return [p.strip() for p in partes if p.strip()]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tradutor de TXT")
        self.geometry("720x560")
        self.minsize(620, 480)

        self.paragrafos = []
        self.fila = queue.Queue()
        self.cancelar = threading.Event()
        self.trabalhando = False

        self.var_entrada = tk.StringVar()
        self.var_saida = tk.StringVar()
        self.var_origem = tk.StringVar(value="Detectar automaticamente")
        self.var_destino = tk.StringVar(value="Português")
        self.var_inicio = tk.IntVar(value=1)
        self.var_qtd = tk.IntVar(value=50)
        self.var_todos = tk.BooleanVar(value=False)
        self.var_anexar = tk.BooleanVar(value=True)
        self.var_info = tk.StringVar(value="Nenhum arquivo carregado.")
        self.assinatura = None

        self._montar_ui()
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)
        self.after(100, self._processar_fila)
        self.after(300, self._oferecer_ultimo_trabalho)

    # ---------- interface ----------
    def _montar_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Arquivo TXT:").grid(row=0, column=0, sticky="w", **pad)
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
        ttk.Label(lim, text="Começar no parágrafo:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(lim, from_=1, to=10_000_000, textvariable=self.var_inicio, width=10).grid(
            row=0, column=1, padx=6, sticky="w")
        ttk.Label(lim, text="Quantidade de parágrafos:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.spin_qtd = ttk.Spinbox(lim, from_=1, to=10_000_000, textvariable=self.var_qtd, width=10)
        self.spin_qtd.grid(row=0, column=3, padx=6, sticky="w")
        ttk.Checkbutton(lim, text="Traduzir até o fim", variable=self.var_todos,
                        command=self._alternar_todos).grid(row=0, column=4, padx=(16, 0))
        ttk.Checkbutton(lim, text="Adicionar ao final do arquivo de saída (continuar tradução em partes)",
                        variable=self.var_anexar).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        botoes = ttk.Frame(frm)
        botoes.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_traduzir = ttk.Button(botoes, text="Traduzir", command=self._iniciar)
        self.btn_traduzir.pack(side="left")
        self.btn_cancelar = ttk.Button(botoes, text="Cancelar", command=self.cancelar.set, state="disabled")
        self.btn_cancelar.pack(side="left", padx=8)
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

    def _escolher_entrada(self):
        caminho = filedialog.askopenfilename(filetypes=[("Arquivos de texto", "*.txt"), ("Todos", "*.*")])
        if not caminho:
            return
        self.var_entrada.set(caminho)
        base, _ = os.path.splitext(caminho)
        self.var_saida.set(f"{base}_traduzido.txt")
        if self._carregar(caminho):
            self._verificar_retomada()

    def _escolher_saida(self):
        caminho = filedialog.asksaveasfilename(defaultextension=".txt",
                                               filetypes=[("Arquivos de texto", "*.txt")])
        if caminho:
            self.var_saida.set(caminho)
            if self.paragrafos:
                self._verificar_retomada()

    def _carregar(self, caminho):
        try:
            self.paragrafos = dividir_paragrafos(ler_texto(caminho))
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao ler o arquivo:\n{e}")
            self.paragrafos = []
            return False
        self.assinatura = assinatura(self.paragrafos)
        total_chars = sum(len(p) for p in self.paragrafos)
        self.var_info.set(f"{len(self.paragrafos)} parágrafos · {total_chars:,} caracteres".replace(",", "."))
        self.var_inicio.set(1)
        self._registrar(f"Carregado: {os.path.basename(caminho)} ({len(self.paragrafos)} parágrafos)")
        return True

    # ---------- retomada ----------
    def _progresso_valido(self, saida):
        prog = ler_progresso(saida)
        if not prog or prog.get("assinatura") != self.assinatura:
            return None
        if not (1 < prog.get("proximo", 0) <= len(self.paragrafos)):
            return None
        return prog

    def _aplicar_progresso(self, prog):
        self.var_inicio.set(prog["proximo"])
        self.var_anexar.set(True)
        if prog.get("origem") in IDIOMAS:
            self.var_origem.set(prog["origem"])
        if prog.get("destino") in IDIOMAS:
            self.var_destino.set(prog["destino"])
        self._registrar(f"Retomando do parágrafo {prog['proximo']} de {len(self.paragrafos)}.")

    def _verificar_retomada(self):
        prog = self._progresso_valido(self.var_saida.get().strip())
        if prog and messagebox.askyesno(
                "Tradução em andamento",
                f"Este arquivo já foi traduzido até o parágrafo {prog['proximo'] - 1} "
                f"de {len(self.paragrafos)}.\n\nContinuar de onde parou?"):
            self._aplicar_progresso(prog)

    def _oferecer_ultimo_trabalho(self):
        saida = ultimo_trabalho()
        prog = ler_progresso(saida) if saida else None
        if not prog or not os.path.isfile(prog.get("entrada", "")):
            return
        if not messagebox.askyesno(
                "Tradução interrompida",
                f"A última tradução não terminou:\n\n{os.path.basename(prog['entrada'])}\n"
                f"Parou no parágrafo {prog['proximo'] - 1} de {prog.get('total', '?')}.\n\n"
                "Continuar de onde parou?"):
            return
        self.var_entrada.set(prog["entrada"])
        self.var_saida.set(saida)
        if not self._carregar(prog["entrada"]):
            return
        prog = self._progresso_valido(saida)
        if prog:
            self._aplicar_progresso(prog)
        else:
            messagebox.showwarning("Atenção", "O arquivo original foi alterado desde a última tradução; "
                                              "não é possível retomar automaticamente.")

    def _ao_fechar(self):
        if self.trabalhando and not messagebox.askyesno(
                "Sair", "Há uma tradução em andamento. O progresso já traduzido fica salvo e "
                        "poderá ser retomado depois.\n\nSair mesmo assim?"):
            return
        self.destroy()

    # ---------- tradução ----------
    def _iniciar(self):
        if self.trabalhando:
            return
        entrada, saida = self.var_entrada.get().strip(), self.var_saida.get().strip()
        if not entrada or not os.path.isfile(entrada):
            messagebox.showwarning("Atenção", "Escolha um arquivo TXT válido.")
            return
        if not saida:
            messagebox.showwarning("Atenção", "Informe onde salvar a tradução.")
            return
        if not self.paragrafos:
            self._carregar(entrada)
            if not self.paragrafos:
                return
        try:
            inicio = int(self.var_inicio.get())
            qtd = None if self.var_todos.get() else int(self.var_qtd.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Atenção", "Valores de parágrafo inválidos.")
            return
        if inicio < 1 or inicio > len(self.paragrafos):
            messagebox.showwarning("Atenção", f"O parágrafo inicial deve estar entre 1 e {len(self.paragrafos)}.")
            return
        if qtd is not None and qtd < 1:
            messagebox.showwarning("Atenção", "A quantidade deve ser pelo menos 1.")
            return

        fim = len(self.paragrafos) if qtd is None else min(len(self.paragrafos), inicio - 1 + qtd)
        selecao = self.paragrafos[inicio - 1:fim]
        origem = IDIOMAS[self.var_origem.get()]
        destino = IDIOMAS[self.var_destino.get()]

        self.trabalhando = True
        self.cancelar.clear()
        self.btn_traduzir.configure(state="disabled")
        self.btn_cancelar.configure(state="normal")
        self.progresso.configure(maximum=len(selecao), value=0)
        self._registrar(f"Traduzindo parágrafos {inicio} a {fim} ({len(selecao)})...")

        info = {
            "entrada": os.path.abspath(entrada),
            "assinatura": self.assinatura,
            "total": len(self.paragrafos),
            "origem": self.var_origem.get(),
            "destino": self.var_destino.get(),
        }
        threading.Thread(target=self._trabalho,
                         args=(selecao, inicio, saida, origem, destino, self.var_anexar.get(), info),
                         daemon=True).start()

    def _trabalho(self, selecao, inicio, saida, origem, destino, anexar, info):
        feitos = 0
        try:
            tradutor = Tradutor(origem, destino)
            if anexar and os.path.exists(saida):
                # Se a última execução travou no meio de um bloco, descarta o trecho incompleto
                prog = ler_progresso(saida)
                if (prog and prog.get("assinatura") == info["assinatura"]
                        and prog.get("proximo") == inicio
                        and os.path.getsize(saida) > prog.get("tamanho_saida", 0)):
                    with open(saida, "r+b") as f:
                        f.truncate(prog["tamanho_saida"])
            else:
                open(saida, "wb").close()

            with open(saida, "ab") as f:
                def registrar_progresso():
                    f.flush()
                    os.fsync(f.fileno())
                    salvar_progresso(saida, {**info, "proximo": inicio + feitos, "tamanho_saida": f.tell()})

                registrar_progresso()
                for lote in montar_lotes(selecao):
                    if self.cancelar.is_set():
                        break
                    traducoes = tradutor.lote(lote)
                    for t in traducoes:
                        f.write((("" if f.tell() == 0 else SEP) + t).encode("utf-8"))
                    feitos += len(lote)
                    registrar_progresso()
                    self.fila.put(("progresso", feitos))

            if inicio + feitos > info["total"]:
                apagar_progresso(saida)
            self.fila.put(("fim", (inicio, feitos, saida, self.cancelar.is_set())))
        except Exception as e:
            self.fila.put(("erro", (inicio, feitos, str(e))))

    def _processar_fila(self):
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "progresso":
                    self.progresso.configure(value=dado)
                elif tipo == "fim":
                    inicio, feitos, saida, cancelado = dado
                    self._finalizar(inicio, feitos)
                    estado = "Cancelado" if cancelado else "Concluído"
                    self._registrar(f"{estado}: {feitos} parágrafos traduzidos → {saida}")
                elif tipo == "erro":
                    inicio, feitos, msg = dado
                    self._finalizar(inicio, feitos)
                    self._registrar(f"ERRO após {feitos} parágrafos: {msg}")
                    messagebox.showerror("Erro na tradução", msg)
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)

    def _finalizar(self, inicio, feitos):
        self.trabalhando = False
        self.btn_traduzir.configure(state="normal")
        self.btn_cancelar.configure(state="disabled")
        proximo = inicio + feitos
        if proximo <= len(self.paragrafos):
            self.var_inicio.set(proximo)
            self._registrar(f"Próxima tradução começará no parágrafo {proximo}.")
        else:
            self._registrar("Arquivo inteiro traduzido.")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()
