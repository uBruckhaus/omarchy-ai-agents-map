import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import chess_rules as c  # noqa: E402

KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
POSITION3 = "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"


def perft(pos, depth):
    if depth == 0:
        return 1
    return sum(perft(c.apply(pos, m), depth - 1) for m in c.legal_moves(pos))


def play(*sans):
    pos = c.start()
    for text in sans:
        move = c.parse(pos, text)
        assert move is not None, text
        pos = c.apply(pos, move)
    return pos


class MoveGeneration(unittest.TestCase):
    # The standard perft counts: the number of move sequences of a given length. Any wrong
    # castling, en passant, promotion or check rule changes them.
    def test_start_position(self):
        self.assertEqual([perft(c.start(), d) for d in (1, 2, 3)], [20, 400, 8902])

    def test_kiwipete(self):
        pos = c.from_fen(KIWIPETE)
        self.assertEqual([perft(pos, d) for d in (1, 2)], [48, 2039])

    def test_position3_with_en_passant_and_pins(self):
        pos = c.from_fen(POSITION3)
        self.assertEqual([perft(pos, d) for d in (1, 2, 3)], [14, 191, 2812])


class Fen(unittest.TestCase):
    def test_round_trip(self):
        for fen in (c.START_FEN, KIWIPETE, POSITION3):
            self.assertEqual(c.fen(c.from_fen(fen)), fen)

    def test_after_moves(self):
        self.assertEqual(c.fen(play("e4")), "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")

    def test_bad_fen_is_refused(self):
        for bad in ("", "8/8/8 w - - 0 1", "rnbqkbnr/pppppppp/9/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                    "rnbqkbnr/ppppxppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", c.START_FEN.replace(" w ", " x "),
                    c.START_FEN + " extra", c.START_FEN.replace(" 0 1", " -1 1")):
            with self.assertRaises(c.FenError, msg=bad):
                c.from_fen(bad)

    def test_castling_rights_need_king_and_rook_at_home(self):
        pos = c.from_fen("4k3/8/8/8/8/8/8/4K2R w KQkq - 0 1")
        self.assertEqual(pos.castling, "K")

    def test_problems_of_a_position_set_up_by_hand(self):
        self.assertEqual(c.problems(c.start()), [])
        self.assertIn("White needs exactly one king", c.problems(c.from_fen("4k3/8/8/8/8/8/8/8 w - - 0 1")))
        self.assertIn("Pawns cannot stand on the first or last rank",
                      c.problems(c.from_fen("4k3/8/8/8/8/8/8/P3K3 w - - 0 1")))
        # Black to move while White is in check cannot happen.
        self.assertIn("The side not to move must not be in check",
                      c.problems(c.from_fen("4k3/8/8/8/8/8/4r3/4K3 b - - 0 1")))


class Notation(unittest.TestCase):
    def test_san_and_parse(self):
        pos = play("e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6")
        self.assertEqual(c.san(pos, c.parse(pos, "O-O")), "O-O")
        self.assertEqual(c.parse(pos, "0-0"), c.parse(pos, "e1g1"))
        self.assertIsNone(c.parse(pos, "Ke2x"))
        self.assertIsNone(c.parse(pos, "Qd3"), "the queen on d1 is blocked by the d2 pawn")

    def test_check_and_mate_marks(self):
        pos = play("f3", "e5", "g4")
        mate = c.parse(pos, "Qh4")
        self.assertEqual(c.san(pos, mate), "Qh4#")
        after = c.apply(pos, mate)
        self.assertEqual(c.status(after), "checkmate")

    def test_disambiguation(self):
        pos = c.from_fen("4k3/8/8/8/8/8/4K3/R6R w - - 0 1")
        self.assertEqual(c.san(pos, c.parse(pos, "a1d1")), "Rad1")
        self.assertEqual(c.parse(pos, "Rhd1"), c.parse(pos, "h1d1"))
        pos = c.from_fen("4k3/R7/8/8/8/8/8/R3K3 w - - 0 1")
        self.assertEqual(c.san(pos, c.parse(pos, "a1a4")), "R1a4")

    def test_promotion_and_en_passant(self):
        pos = c.from_fen("4k3/1P6/8/8/8/8/8/4K3 w - - 0 1")
        self.assertEqual(c.san(pos, c.parse(pos, "b8=Q")), "b8=Q+")
        self.assertEqual(c.parse(pos, "b8Q"), c.parse(pos, "b7b8q"))
        pos = play("e4", "a6", "e5", "d5")
        move = c.parse(pos, "exd6")
        self.assertEqual(c.san(pos, move), "exd6")
        self.assertEqual(c.apply(pos, move).at(c.square("d5")), "")

    def test_stalemate(self):
        pos = c.from_fen("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertEqual(c.status(pos), "stalemate")

    def test_game_san(self):
        start = c.start()
        moves, pos = [], start
        for text in ("e4", "e5", "Nf3"):
            move = c.parse(pos, text)
            moves.append(move)
            pos = c.apply(pos, move)
        self.assertEqual(c.game_san(start, moves), "1. e4 e5 2. Nf3")
        black_start = c.from_fen("4k3/8/8/8/8/8/4P3/4K3 b - - 0 7")
        self.assertEqual(c.game_san(black_start, [c.parse(black_start, "Kd7")]), "7... Kd7")


class Captured(unittest.TestCase):
    def test_captures_in_order_with_en_passant(self):
        start, moves, pos = c.start(), [], c.start()
        for text in ("e4", "d5", "exd5", "Qxd5", "Nc3", "a6", "Nxd5", "c5", "d4", "c4", "b4", "cxb3"):
            move = c.parse(pos, text)
            moves.append(move)
            pos = c.apply(pos, move)
        self.assertEqual(c.captured(start, moves), ["p", "P", "q", "P"])   # the last one en passant


class AnswersOfAgents(unittest.TestCase):
    def setUp(self):
        self.pos = play("e4")

    def find(self, answer):
        move, token = c.find_move(self.pos, answer)
        return c.san(self.pos, move) if move else None, token

    def test_first_line_wins(self):
        self.assertEqual(self.find("e5\nA classical answer; Nf6 was possible too.")[0], "e5")

    def test_markdown_and_move_numbers(self):
        self.assertEqual(self.find("**1... c5** — the Sicilian.")[0], "c5")
        self.assertEqual(self.find("`Nf6`")[0], "Nf6")

    def test_square_before_a_full_stop(self):
        self.assertEqual(self.find("I answer with e5.")[0], "e5")

    def test_uci_and_illegal_moves(self):
        self.assertEqual(self.find("g8f6")[0], "Nf6")
        self.assertEqual(self.find("Qh4 would be nice but I play d5")[0], "d5")

    def test_no_move(self):
        self.assertEqual(self.find("I cannot play chess today."), (None, ""))
        self.assertEqual(self.find(""), (None, ""))
        self.assertEqual(self.find("x" * 100000), (None, ""))


if __name__ == "__main__":
    unittest.main()
