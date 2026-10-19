"""Chess rules for the chess page: positions, legal moves, FEN, SAN and UCI.

Pure Python, no GTK, so it is tested on its own (tests/test_chess_rules.py). A square is an
index 0..63, a1 = 0, h1 = 7, a8 = 56. A piece is a letter, upper case for White
("PNBRQK"), lower case for Black; an empty square is "". Positions never change: a move
gives a new one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

FILES = "abcdefgh"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
KNIGHT = ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2))
KING = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
ROOK_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
BISHOP_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
MAX_TEXT = 4000          # how much of an answer is searched for a move


class FenError(ValueError):
    pass


def square(name: str) -> int:
    return FILES.index(name[0]) + 8 * (int(name[1]) - 1)


def name(sq: int) -> str:
    return FILES[sq % 8] + str(sq // 8 + 1)


def white(piece: str) -> bool:
    return piece.isupper()


def color_of(piece: str) -> str:
    return "w" if piece.isupper() else "b"


@dataclass(frozen=True)
class Move:
    start: int
    end: int
    promotion: str = ""          # "q", "r", "b" or "n" (always lower case here)

    def uci(self) -> str:
        return name(self.start) + name(self.end) + self.promotion


@dataclass(frozen=True)
class Position:
    board: tuple                 # 64 pieces ("" for empty)
    turn: str = "w"
    castling: str = "KQkq"       # "" when nobody may castle
    en_passant: int | None = None
    halfmove: int = 0
    fullmove: int = 1

    def at(self, sq: int) -> str:
        return self.board[sq]


# --- FEN -------------------------------------------------------------------------------------------

def from_fen(fen: str) -> Position:
    """A position from FEN; a malformed one raises FenError. Only the placement is required."""
    parts = str(fen or "").strip().split()
    if not 1 <= len(parts) <= 6:
        raise FenError("FEN has the wrong number of fields")
    rows = parts[0].split("/")
    if len(rows) != 8:
        raise FenError("FEN needs eight ranks")
    board = [""] * 64
    for r, row in enumerate(rows):
        rank = 7 - r
        f = 0
        for ch in row:
            if ch.isdigit():
                f += int(ch)
            elif ch in "PNBRQKpnbrqk":
                if f > 7:
                    raise FenError("a rank has more than eight squares")
                board[rank * 8 + f] = ch
                f += 1
            else:
                raise FenError(f"unknown piece “{ch}”")
        if f != 8:
            raise FenError("a rank does not have eight squares")
    turn = parts[1] if len(parts) > 1 else "w"
    if turn not in ("w", "b"):
        raise FenError("side to move must be w or b")
    castling = parts[2] if len(parts) > 2 else "-"
    if castling != "-" and not re.fullmatch(r"K?Q?k?q?", castling):
        raise FenError("bad castling field")
    ep = parts[3] if len(parts) > 3 else "-"
    if ep != "-" and not re.fullmatch(r"[a-h][36]", ep):
        raise FenError("bad en passant field")
    try:
        half = int(parts[4]) if len(parts) > 4 else 0
        full = int(parts[5]) if len(parts) > 5 else 1
    except ValueError:
        raise FenError("move counters must be numbers") from None
    if not (0 <= half <= 10000 and 1 <= full <= 10000):
        raise FenError("move counters out of range")
    pos = Position(tuple(board), turn, "" if castling == "-" else castling,
                   None if ep == "-" else square(ep), half, full)
    return replace(pos, castling=_possible_castling(pos))


def fen(pos: Position) -> str:
    rows = []
    for rank in range(7, -1, -1):
        row, empty = "", 0
        for f in range(8):
            piece = pos.board[rank * 8 + f]
            if piece:
                row += (str(empty) if empty else "") + piece
                empty = 0
            else:
                empty += 1
        rows.append(row + (str(empty) if empty else ""))
    ep = name(pos.en_passant) if pos.en_passant is not None else "-"
    return f"{'/'.join(rows)} {pos.turn} {pos.castling or '-'} {ep} {pos.halfmove} {pos.fullmove}"


def start() -> Position:
    return from_fen(START_FEN)


def _possible_castling(pos: Position) -> str:
    """Only the castling rights whose king and rook still stand at home."""
    keep = ""
    for flag, king_sq, rook_sq, king, rook in (("K", 4, 7, "K", "R"), ("Q", 4, 0, "K", "R"),
                                                 ("k", 60, 63, "k", "r"), ("q", 60, 56, "k", "r")):
        if flag in pos.castling and pos.board[king_sq] == king and pos.board[rook_sq] == rook:
            keep += flag
    return keep


def problems(pos: Position) -> list[str]:
    """Why a position set up by hand cannot be played (empty when it can)."""
    out = []
    for king, side in (("K", "White"), ("k", "Black")):
        count = pos.board.count(king)
        if count != 1:
            out.append(f"{side} needs exactly one king")
    if any(pos.board[sq] in ("P", "p") for sq in list(range(8)) + list(range(56, 64))):
        out.append("Pawns cannot stand on the first or last rank")
    for side, piece in (("w", "P"), ("b", "p")):
        if pos.board.count(piece) > 8:
            out.append("A side cannot have more than eight pawns")
    if not out:
        other = "b" if pos.turn == "w" else "w"
        if in_check(pos, other):
            out.append("The side not to move must not be in check")
    return out


# --- moves -----------------------------------------------------------------------------------------

def _step(sq: int, df: int, dr: int) -> int | None:
    f, r = sq % 8 + df, sq // 8 + dr
    return r * 8 + f if 0 <= f < 8 and 0 <= r < 8 else None


def attacked(pos: Position, sq: int, by: str) -> bool:
    """Is the square attacked by side `by` ("w" or "b")?"""
    board = pos.board

    def mine(piece, kinds):
        return piece and color_of(piece) == by and piece.upper() in kinds

    pawn_dr = -1 if by == "w" else 1          # a white pawn attacks from one rank below
    for df in (-1, 1):
        t = _step(sq, df, pawn_dr)
        if t is not None and mine(board[t], "P"):
            return True
    for df, dr in KNIGHT:
        t = _step(sq, df, dr)
        if t is not None and mine(board[t], "N"):
            return True
    for df, dr in KING:
        t = _step(sq, df, dr)
        if t is not None and mine(board[t], "K"):
            return True
    for dirs, kinds in ((ROOK_DIRS, "RQ"), (BISHOP_DIRS, "BQ")):
        for df, dr in dirs:
            t = _step(sq, df, dr)
            while t is not None:
                if board[t]:
                    if mine(board[t], kinds):
                        return True
                    break
                t = _step(t, df, dr)
    return False


def in_check(pos: Position, side: str) -> bool:
    king = "K" if side == "w" else "k"
    try:
        return attacked(pos, pos.board.index(king), "b" if side == "w" else "w")
    except ValueError:
        return False


def _pseudo(pos: Position):
    board, side = pos.board, pos.turn
    for sq, piece in enumerate(board):
        if not piece or color_of(piece) != side:
            continue
        kind = piece.upper()
        if kind == "P":
            dr = 1 if side == "w" else -1
            last = 7 if side == "w" else 0
            home = 1 if side == "w" else 6
            one = _step(sq, 0, dr)
            if one is not None and not board[one]:
                yield from _pawn(sq, one, last)
                two = _step(one, 0, dr)
                if sq // 8 == home and two is not None and not board[two]:
                    yield Move(sq, two)
            for df in (-1, 1):
                t = _step(sq, df, dr)
                if t is None:
                    continue
                if board[t] and color_of(board[t]) != side:
                    yield from _pawn(sq, t, last)
                elif t == pos.en_passant:
                    yield Move(sq, t)
        elif kind in "NK":
            for df, dr in (KNIGHT if kind == "N" else KING):
                t = _step(sq, df, dr)
                if t is not None and (not board[t] or color_of(board[t]) != side):
                    yield Move(sq, t)
            if kind == "K":
                yield from _castles(pos, sq)
        else:
            dirs = ROOK_DIRS if kind == "R" else BISHOP_DIRS if kind == "B" else ROOK_DIRS + BISHOP_DIRS
            for df, dr in dirs:
                t = _step(sq, df, dr)
                while t is not None:
                    if board[t]:
                        if color_of(board[t]) != side:
                            yield Move(sq, t)
                        break
                    yield Move(sq, t)
                    t = _step(t, df, dr)


def _pawn(start_sq: int, end: int, last: int):
    if end // 8 == last:
        for p in "qrbn":
            yield Move(start_sq, end, p)
    else:
        yield Move(start_sq, end)


def _castles(pos: Position, king_sq: int):
    side, other = pos.turn, "b" if pos.turn == "w" else "w"
    home = 4 if side == "w" else 60
    if king_sq != home or attacked(pos, home, other):
        return
    short, long_ = ("K", "Q") if side == "w" else ("k", "q")
    if short in pos.castling and not pos.board[home + 1] and not pos.board[home + 2] \
            and not attacked(pos, home + 1, other) and not attacked(pos, home + 2, other):
        yield Move(home, home + 2)
    if long_ in pos.castling and not pos.board[home - 1] and not pos.board[home - 2] and not pos.board[home - 3] \
            and not attacked(pos, home - 1, other) and not attacked(pos, home - 2, other):
        yield Move(home, home - 2)


def apply(pos: Position, move: Move) -> Position:
    """The position after a move (assumed legal: see legal_moves)."""
    board = list(pos.board)
    piece = board[move.start]
    captured = board[move.end]
    board[move.start] = ""
    ep = None
    if piece.upper() == "P":
        if move.end == pos.en_passant and not captured:
            board[move.end + (-8 if piece == "P" else 8)] = ""
            captured = "p"
        if abs(move.end - move.start) == 16:
            ep = (move.start + move.end) // 2
        if move.promotion:
            piece = move.promotion.upper() if piece == "P" else move.promotion
    if piece.upper() == "K" and abs(move.end - move.start) == 2:
        rook_from, rook_to = (move.start + 3, move.start + 1) if move.end > move.start else (move.start - 4, move.start - 1)
        board[rook_to], board[rook_from] = board[rook_from], ""
    board[move.end] = piece
    castling = pos.castling
    for sq, flags in ((4, "KQ"), (60, "kq"), (0, "Q"), (7, "K"), (56, "q"), (63, "k")):
        if move.start == sq or move.end == sq:
            castling = "".join(c for c in castling if c not in flags)
    half = 0 if piece.upper() == "P" or captured else pos.halfmove + 1
    full = pos.fullmove + (1 if pos.turn == "b" else 0)
    return Position(tuple(board), "b" if pos.turn == "w" else "w", castling, ep, half, full)


def legal_moves(pos: Position) -> list[Move]:
    out = []
    for move in _pseudo(pos):
        if not in_check(apply(pos, move), pos.turn):
            out.append(move)
    return out


def status(pos: Position) -> str:
    """"checkmate", "stalemate", "fifty" (fifty-move rule), "material" (no mate possible) or "ongoing"."""
    if not legal_moves(pos):
        return "checkmate" if in_check(pos, pos.turn) else "stalemate"
    if pos.halfmove >= 100:
        return "fifty"
    rest = sorted(p.upper() for p in pos.board if p and p.upper() != "K")
    if rest in ([], ["B"], ["N"]):
        return "material"
    return "ongoing"


# --- notation --------------------------------------------------------------------------------------

def san(pos: Position, move: Move) -> str:
    """Standard algebraic notation of a legal move, with + or #."""
    piece = pos.at(move.start)
    kind = piece.upper()
    if kind == "K" and abs(move.end - move.start) == 2:
        text = "O-O" if move.end > move.start else "O-O-O"
    else:
        capture = bool(pos.at(move.end)) or (kind == "P" and move.end == pos.en_passant)
        if kind == "P":
            text = (FILES[move.start % 8] + "x" if capture else "") + name(move.end)
            if move.promotion:
                text += "=" + move.promotion.upper()
        else:
            others = [m for m in legal_moves(pos) if m.end == move.end and m.start != move.start
                      and pos.at(m.start) == piece]
            hint = ""
            if others:
                if all(m.start % 8 != move.start % 8 for m in others):
                    hint = FILES[move.start % 8]
                elif all(m.start // 8 != move.start // 8 for m in others):
                    hint = str(move.start // 8 + 1)
                else:
                    hint = name(move.start)
            text = kind + hint + ("x" if capture else "") + name(move.end)
    after = apply(pos, move)
    if in_check(after, after.turn):
        text += "#" if not legal_moves(after) else "+"
    return text


def _plain(text: str) -> str:
    return re.sub(r"[+#!?]+$", "", text.strip()).replace("0-0-0", "O-O-O").replace("0-0", "O-O")


def parse(pos: Position, text: str) -> Move | None:
    """A legal move from SAN ("Nf3", "exd5", "e8=Q", "O-O") or UCI ("g1f3", "e7e8q"); None otherwise."""
    word = _plain(str(text or ""))
    if not word or len(word) > 12:
        return None
    moves = legal_moves(pos)
    low = word.lower()
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", low):
        for m in moves:
            if m.uci() == low:
                return m
    for m in moves:
        if _plain(san(pos, m)) == word:
            return m
    # A promotion written without "=" ("e8Q") or a pawn capture with a lower-case piece letter.
    if re.fullmatch(r"[a-h](x[a-h])?[18][QRBN]", word):
        return parse(pos, word[:-1] + "=" + word[-1])
    return None


TOKEN = re.compile(r"O-O-O|O-O|0-0-0|0-0|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBNqrbn])?[+#]?|[a-h][1-8][a-h][1-8][qrbn]?")


def find_move(pos: Position, answer: str) -> tuple[Move | None, str]:
    """The move an agent's answer names: the first legal move in it, its first line first.
    Returns (move, the text it was written as); (None, "") when the answer names none."""
    text = str(answer or "")[:MAX_TEXT]
    lines = [line for line in text.splitlines() if line.strip()]
    for chunk in (lines[:1], lines[1:]):
        for line in chunk:
            # Move numbers ("12." or "12...") go; a square before a full stop ("e4.") stays.
            cleaned = re.sub(r"(?<![A-Za-z])\d+\s*\.(\.\.)?", " ", line.replace("*", " ").replace("`", " "))
            for token in TOKEN.findall(cleaned):
                move = parse(pos, token)
                if move is not None:
                    return move, token
    return None, ""


def captured(start: Position, moves: list) -> list[str]:
    """The pieces taken in the game so far, in order ("p" a black pawn, "Q" a white queen; en passant too)."""
    taken, pos = [], start
    for move in moves:
        piece, target = pos.at(move.start), pos.at(move.end)
        if target:
            taken.append(target)
        elif piece.lower() == "p" and move.start % 8 != move.end % 8:
            taken.append("p" if piece == "P" else "P")       # en passant: the pawn beside it
        pos = apply(pos, move)
    return taken


def game_san(start_pos: Position, moves: list[Move]) -> str:
    """The moves of a game as numbered SAN ("1. e4 e5 2. Nf3"), from its start position."""
    pos, parts = start_pos, []
    for i, move in enumerate(moves):
        if pos.turn == "w":
            parts.append(f"{pos.fullmove}.")
        elif i == 0:
            parts.append(f"{pos.fullmove}...")
        parts.append(san(pos, move))
        pos = apply(pos, move)
    return " ".join(parts)
