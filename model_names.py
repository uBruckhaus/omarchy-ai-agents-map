"""What a model's name says: the parts of a Hugging Face repository or GGUF file name
("Qwen3.6-35B-A3B-UD-Q4_K_XL", "gemma-4-12b-it-qat") and one line about each, for
people new to local models. Pure text work, nothing is read or fetched.

parts(name) -> [(the part as written, explanation)], in the order they appear,
each kind once. Explanations are English msgids (N_); the page translates them.
"""

import re

from i18n import N_

# Quantisation families, most specific first: (pattern, explanation).
QUANTS = [
    (r"(?:BF16|F16|FP16)", N_("Not quantised (16 bit): the original quality, the largest file.")),
    (r"(?:F32|FP32)", N_("Not quantised (32 bit): twice the size of 16 bit, no better to use.")),
    (r"MXFP4", N_("MXFP4: a 4-bit floating-point format (OpenAI's gpt-oss models ship in it).")),
    (r"NVFP4", N_("NVFP4: NVIDIA's 4-bit floating-point format, fastest on its newest cards.")),
    (r"IQ[12]_[A-Z]+(?:_[A-Z]+)*", N_("{0}: an i-quant with 1–2 bits per weight: very small, clearly weaker; "
                                  "only when nothing else fits.")),
    (r"IQ3_[A-Z]+(?:_[A-Z]+)*", N_("{0}: an i-quant with about 3 bits per weight: small, a bit weaker.")),
    (r"IQ4_[A-Z]+(?:_[A-Z]+)*", N_("{0}: an i-quant with about 4 bits per weight: smaller than Q4_K_M at similar "
                       "quality, can be a little slower.")),
    (r"Q8_0", N_("Q8_0: 8 bits per weight, practically as good as the original at half the size.")),
    (r"Q6_K(?:_[A-Z]+)?", N_("{0}: 6 bits per weight, hardly any loss.")),
    (r"Q5_K_[A-Z]+|Q5_[01]", N_("{0}: 5 bits per weight, very good quality.")),
    (r"Q4_K_M", N_("Q4_K_M: about 4.8 bits per weight, the usual choice: good quality at about a quarter "
                   "of the original size.")),
    (r"Q4_K_[A-Z]+|Q4_[01]", N_("{0}: about 4 bits per weight, good quality.")),
    (r"Q3_K_[A-Z]+", N_("{0}: 3 bits per weight, noticeably weaker; for tight memory.")),
    (r"Q2_K(?:_[A-Z]+)?", N_("{0}: 2 bits per weight, much weaker; only when nothing else fits.")),
]
# Words (case-insensitive, whole parts of the name): (pattern, explanation).
WORDS = [
    (r"UD", N_("UD: Unsloth Dynamic, important layers kept at higher precision, better quality at the "
               "same size.")),
    (r"MTP", N_("MTP: a multi-token-prediction head, it guesses several tokens at once: faster answers "
                "where the local LLM supports it (recent llama.cpp).")),
    (r"QAT", N_("QAT: trained for quantisation, it loses less quality at 4 bit than a normal "
                "quantisation.")),
    (r"i1", N_("i1: made with an importance matrix, better quality at the same size.")),
    (r"imatrix", N_("imatrix: made with an importance matrix, better quality at the same size.")),
    (r"(?:Instruct|it|Chat)", N_("{0}: tuned to follow instructions and to chat.")),
    (r"Base", N_("Base: not tuned for chat; a starting point for further training.")),
    (r"(?:Thinking|Reasoning|Think|R1)", N_("{0}: thinks before it answers: better at hard tasks, slower.")),
    (r"(?:Coder|Code)", N_("{0}: trained for programming.")),
    (r"(?:VL|Vision|V)", N_("{0}: understands images too (needs its mmproj file).")),
    (r"(?:abliterated|uncensored|heretic)", N_("{0}: refusals removed: it answers what the original "
                                                "would refuse.")),
    (r"(?:distill|distilled)", N_("{0}: a smaller model trained on a larger model's answers.")),
    (r"mmproj", N_("mmproj: the vision projector, the part that lets a model read images.")),
    (r"GGUF", N_("GGUF: the file format of llama.cpp, LM Studio, Ollama and KoboldCpp.")),
]
SIZE_MOE = re.compile(r"^(\d+(?:\.\d+)?)[Bb]-?[Aa](\d+(?:\.\d+)?)[Bb]$")    # 35B-A3B (joined below)
SIZE = re.compile(r"^(\d+(?:\.\d+)?)[Bb]$")
SIZE_E = re.compile(r"^[Ee](\d+(?:\.\d+)?)[Bb]$")
ACTIVE = re.compile(r"^[Aa](\d+(?:\.\d+)?)[Bb]$")
CONTEXT = re.compile(r"^(\d+)[Kk]$|^(\d+)[Mm]$")
MOE = N_("{0}: a mixture of experts with {1} billion parameters in all, of which {2} billion work for each "
         "token: fast for its size, but all of it must fit in memory.")
PARAMS = N_("{0}: {1} billion parameters. More knows more and needs more memory.")
EFFECTIVE = N_("{0}: about {1} billion effective parameters (Gemma's E sizes: fewer work per token).")
CONTEXT_TEXT = N_("{0}: a context window of {1} tokens.")


def tokens(name: str) -> list[str]:
    """The name cut at - _ . / and spaces, the quantisation kept whole (Q4_K_M), ".gguf" dropped."""
    name = re.sub(r"\.gguf$", "", name.split("/")[-1], flags=re.IGNORECASE)
    quant = r"I?Q\d_[A-Z0-9]+(?:_[A-Z0-9]+)*|BF16|F16|F32|FP16|FP32|MXFP4|NVFP4"
    found = []
    for piece in re.split(rf"({quant})", name, flags=re.IGNORECASE):
        if not piece:
            continue
        if re.fullmatch(quant, piece, flags=re.IGNORECASE):
            found.append(piece.upper())
        else:
            found += [p for p in re.split(r"[-_.\s/]+", piece) if p]
    return found


def parts(name: str) -> list[tuple[str, str, tuple]]:
    """[(part, explanation msgid, values for its {1} {2} ...)]: each kind of part once."""
    words = tokens(name)
    found, kinds = [], set()

    def add(kind, part, text, *values):
        if kind not in kinds:
            kinds.add(kind)
            found.append((part, text, values))

    i = 0
    while i < len(words):
        word = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if SIZE.fullmatch(word) and ACTIVE.fullmatch(nxt):
            add("size", f"{word}-{nxt}", MOE, SIZE.fullmatch(word).group(1), ACTIVE.fullmatch(nxt).group(1))
            i += 2
            continue
        if SIZE_MOE.fullmatch(word):
            m = SIZE_MOE.fullmatch(word)
            add("size", word, MOE, m.group(1), m.group(2))
        elif SIZE.fullmatch(word):
            add("size", word, PARAMS, SIZE.fullmatch(word).group(1))
        elif SIZE_E.fullmatch(word):
            add("size", word, EFFECTIVE, SIZE_E.fullmatch(word).group(1))
        elif CONTEXT.fullmatch(word) and int(re.sub(r"\D", "", word) or 0) in (32, 64, 128, 256, 512, 1, 2, 4):
            m = CONTEXT.fullmatch(word)
            count = int(m.group(1)) * 1000 if m.group(1) else int(m.group(2)) * 1_000_000
            tokens_text = f"{count:,}".replace(",", " ")
            add("context", word, CONTEXT_TEXT, tokens_text)
        else:
            for pattern, text in QUANTS:
                if re.fullmatch(pattern, word):
                    add("quant", word, text)
                    break
            else:
                for pattern, text in WORDS:
                    if re.fullmatch(pattern, word, flags=re.IGNORECASE):
                        add(pattern, word, text)
                        break
        i += 1
    return found
