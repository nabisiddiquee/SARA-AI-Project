"""Small built-in games for Sara.

Sara dispatches one command at a time (stateless), so the multi-turn number
guessing game keeps its state in a module-level object. The single-shot games
(dice, coin) need no state.

All replies are short, plain text, and bilingual (Hindi/Hinglish or English)
so they read well through the text-to-speech engine.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# Range for the number guessing game (inclusive).
_LOW = 1
_HIGH = 100

# Keywords that start the guessing game.
_GAME_START_KWS = (
    "guessing game", "guess game", "number game", "guess the number",
    "number guessing", "guessing", "khel khelo", "game khelo", "guess karo",
    "number guess", "andaaz", "andaz", "अंदाज़", "अंदाज", "खेल खेलो", "गेम खेलो",
)

# Keywords that quit an in-progress game.
_GAME_STOP_KWS = (
    "stop game", "quit game", "end game", "give up", "haar", "chhodo", "chodo",
    "bas karo", "khel band", "game band", "गेम बंद", "खेल बंद", "हार",
)

# Single-shot game triggers.
_DICE_KWS = ("roll dice", "roll a dice", "roll the dice", "dice roll", "throw dice",
             "pasa", "paasa", "पासा", "डाइस")
_COIN_KWS = ("flip coin", "flip a coin", "toss coin", "toss a coin", "coin toss",
             "coin flip", "sikka uchalo", "sikka", "toss karo", "सिक्का", "टॉस")

# Rock-paper-scissors: the three moves plus a general "let's play" trigger.
_RPS_ROCK = ("rock", "patthar", "pathar", "पत्थर")
_RPS_PAPER = ("paper", "kagaz", "kaagaz", "कागज़", "कागज")
_RPS_SCISSORS = ("scissors", "scissor", "kainchi", "kenchi", "कैंची")
_RPS_GAME_KWS = ("rock paper scissors", "rock paper scissor", "rps",
                 "patthar kagaz", "pathar kagaz", "पत्थर कागज़")
_RPS_KWS = _RPS_ROCK + _RPS_PAPER + _RPS_SCISSORS + _RPS_GAME_KWS

# What each move beats (for deciding the winner).
_RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
_RPS_LABEL = {
    "rock": ("पत्थर", "Rock"),
    "paper": ("कागज़", "Paper"),
    "scissors": ("कैंची", "Scissors"),
}

# Quiz game: start triggers and a small bilingual general-knowledge question
# bank. Each question is asked in the player's language; ``accept`` lists the
# lowercase answer fragments treated as correct (English + Hindi + romanized).
_QUIZ_START_KWS = ("quiz", "trivia", "quiz khelo", "quiz game", "sawaal jawab",
                   "क्विज़", "क्विज", "प्रश्नोत्तरी", "सवाल जवाब")

_QUIZ_QUESTIONS = (
    {
        "q_hi": "भारत की राजधानी क्या है?",
        "q_en": "What is the capital of India?",
        "answer": "New Delhi",
        "accept": ("new delhi", "delhi", "दिल्ली", "नई दिल्ली"),
    },
    {
        "q_hi": "हमारे सौरमंडल का सबसे बड़ा ग्रह कौन सा है?",
        "q_en": "Which is the largest planet in our solar system?",
        "answer": "Jupiter",
        "accept": ("jupiter", "बृहस्पति", "बृहस्पति ग्रह", "guru"),
    },
    {
        "q_hi": "पानी का रासायनिक सूत्र क्या है?",
        "q_en": "What is the chemical formula for water?",
        "answer": "H2O",
        "accept": ("h2o", "h 2 o", "एच2ओ"),
    },
    {
        "q_hi": "एक सप्ताह में कितने दिन होते हैं?",
        "q_en": "How many days are there in a week?",
        "answer": "7",
        "accept": ("7", "seven", "saat", "सात"),
    },
    {
        "q_hi": "भारत का राष्ट्रीय पशु कौन सा है?",
        "q_en": "What is the national animal of India?",
        "answer": "Tiger",
        "accept": ("tiger", "बाघ", "bagh", "sher", "शेर"),
    },
    {
        "q_hi": "किस ग्रह को लाल ग्रह कहा जाता है?",
        "q_en": "Which planet is known as the Red Planet?",
        "answer": "Mars",
        "accept": ("mars", "मंगल", "mangal", "मंगल ग्रह"),
    },
)

# How many questions one quiz round asks (capped at the bank size).
_QUIZ_ROUND = 5

# Tic-tac-toe: start triggers. The player is X, Sara is O. Board cells are
# numbered 1-9 like a phone keypad.
_TTT_START_KWS = ("tic tac toe", "tic-tac-toe", "tictactoe", "tick tack toe",
                  "tik tak toe", "tik tok", "tik-tok", "noughts and crosses",
                  "x o game", "xo game", "टिक टैक टो", "टिकटैकटो")

# The eight winning lines (board indices 0-8).
_TTT_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),  # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),  # columns
    (0, 4, 8), (2, 4, 6),             # diagonals
)


@dataclass
class _GuessGame:
    secret: int
    tries: int = 0


@dataclass
class _QuizGame:
    order: list[int]  # indices into _QUIZ_QUESTIONS, in the order to ask
    idx: int = 0      # how many questions have been asked so far
    score: int = 0


@dataclass
class _TicTacToe:
    # Nine cells; " " is empty, "X" is the player, "O" is Sara.
    board: list[str]


# The single active guessing game, or None when no game is running.
_active: _GuessGame | None = None

# The single active quiz, or None when no quiz is running.
_quiz: _QuizGame | None = None

# The single active tic-tac-toe game, or None when none is running.
_ttt: _TicTacToe | None = None


def _has(text: str, kws) -> bool:
    """Whole-word (ASCII) / substring (non-ASCII) keyword match.

    Mirrors the matching rules used by the command dispatcher so game triggers
    behave consistently with the rest of Sara.
    """
    padded = " " + " ".join(text.lower().split()) + " "
    low = text.lower()
    for k in kws:
        k = k.lower()
        if k.isascii():
            if f" {k} " in padded:
                return True
        elif k in low:
            return True
    return False


def stop_all() -> None:
    """End every running game (the "sab roko" command)."""
    global _active, _quiz, _ttt
    _active = _quiz = _ttt = None


def is_game_active() -> bool:
    """True while a number guessing game is waiting for the next guess."""
    return _active is not None


def is_game_start(text: str) -> bool:
    """True if ``text`` asks to start the number guessing game."""
    return _has(text, _GAME_START_KWS)


def is_dice(text: str) -> bool:
    return _has(text, _DICE_KWS)


def is_coin(text: str) -> bool:
    return _has(text, _COIN_KWS)


def start_number_game(*, lang: str = "en") -> str:
    """Begin a new number guessing game and return the intro message."""
    global _active
    _active = _GuessGame(secret=random.randint(_LOW, _HIGH))
    return (
        f"चलिए खेलते हैं! मैंने {_LOW} से {_HIGH} के बीच एक नंबर सोचा है। "
        "अंदाज़ा लगाइए — कौन सा नंबर है?"
        if lang == "hi" else
        f"Let's play! I'm thinking of a number between {_LOW} and {_HIGH}. "
        "Take a guess — what is it?"
    )


def _stop_message(lang: str, secret: int) -> str:
    return (
        f"कोई बात नहीं, खेल यहीं रोकते हैं। मेरा नंबर {secret} था।"
        if lang == "hi" else
        f"No worries, let's stop here. My number was {secret}."
    )


def play_number_game(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Process one guess (or a quit request) for the active game.

    Returns ``(ok, message)``. When the guess is correct or the player quits,
    the game ends and the module goes back to having no active game.
    """
    global _active
    if _active is None:  # defensive: caller should check is_game_active() first
        return False, start_number_game(lang=lang)

    if _has(text, _GAME_STOP_KWS):
        secret = _active.secret
        _active = None
        return True, _stop_message(lang, secret)

    match = re.search(r"-?\d+", text)
    if not match:
        return False, (
            "कृपया एक नंबर बताइए, या रोकने के लिए 'बंद करो' कहिए।"
            if lang == "hi" else
            "Please say a number, or say 'stop game' to quit."
        )

    guess = int(match.group())
    _active.tries += 1

    if guess < _LOW or guess > _HIGH:
        return False, (
            f"नंबर {_LOW} से {_HIGH} के बीच होना चाहिए। दोबारा कोशिश करें।"
            if lang == "hi" else
            f"The number should be between {_LOW} and {_HIGH}. Try again."
        )

    if guess < _active.secret:
        return False, (
            "थोड़ा बड़ा नंबर सोचिए!" if lang == "hi" else "Try a higher number!"
        )
    if guess > _active.secret:
        return False, (
            "थोड़ा छोटा नंबर सोचिए!" if lang == "hi" else "Try a lower number!"
        )

    tries = _active.tries
    _active = None
    return True, (
        f"बिल्कुल सही! नंबर {guess} था। आपने {tries} कोशिश में पकड़ लिया।"
        if lang == "hi" else
        f"Correct! The number was {guess}. You got it in {tries} tries."
    )


def roll_dice(*, lang: str = "en") -> tuple[bool, str]:
    """Roll a single six-sided die."""
    value = random.randint(1, 6)
    return True, (
        f"पासा फेंका — {value} आया!" if lang == "hi" else f"You rolled a {value}!"
    )


def flip_coin(*, lang: str = "en") -> tuple[bool, str]:
    """Toss a coin."""
    if random.random() < 0.5:
        return True, ("सिक्का उछाला — हेड आया!" if lang == "hi" else "It's Heads!")
    return True, ("सिक्का उछाला — टेल आया!" if lang == "hi" else "It's Tails!")


def is_rps(text: str) -> bool:
    """True if ``text`` asks to play rock-paper-scissors or throws a move."""
    return _has(text, _RPS_KWS)


def _rps_move(text: str) -> str | None:
    """Extract the player's move from ``text``, or None if only the game name.

    The full game phrase ("rock paper scissors") contains all three move words,
    so treat it as "no move chosen yet" and let the caller prompt the player.
    """
    if _has(text, _RPS_GAME_KWS):
        return None
    if _has(text, _RPS_SCISSORS):  # check first: "scissors" never clashes
        return "scissors"
    if _has(text, _RPS_ROCK):
        return "rock"
    if _has(text, _RPS_PAPER):
        return "paper"
    return None


def play_rps(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Play one round of rock-paper-scissors against ``text``'s move.

    If no move is given (e.g. just "let's play rock paper scissors"), prompt the
    player to pick one instead of guessing for them.
    """
    move = _rps_move(text)
    if move is None:
        return False, (
            "पत्थर, कागज़ या कैंची — कौन सी चाल चलेंगे?"
            if lang == "hi" else
            "Rock, paper, or scissors — what's your move?"
        )

    sara = random.choice(("rock", "paper", "scissors"))
    you_hi, you_en = _RPS_LABEL[move]
    sara_hi, sara_en = _RPS_LABEL[sara]

    if move == sara:
        outcome_hi, outcome_en = "टाई हो गई!", "It's a tie!"
    elif _RPS_BEATS[move] == sara:
        outcome_hi, outcome_en = "आप जीत गए!", "You win!"
    else:
        outcome_hi, outcome_en = "मैं जीत गई!", "I win!"

    if lang == "hi":
        return True, f"आपने {you_hi} चुना, मैंने {sara_hi}। {outcome_hi}"
    return True, f"You picked {you_en}, I picked {sara_en}. {outcome_en}"


def is_quiz_active() -> bool:
    """True while a quiz is waiting for the answer to the current question."""
    return _quiz is not None


def is_quiz_start(text: str) -> bool:
    """True if ``text`` asks to start the quiz game."""
    return _has(text, _QUIZ_START_KWS)


def _quiz_question_text(lang: str) -> str:
    """Format the current quiz question with its number."""
    assert _quiz is not None
    q = _QUIZ_QUESTIONS[_quiz.order[_quiz.idx]]
    n = _quiz.idx + 1
    total = len(_quiz.order)
    if lang == "hi":
        return f"सवाल {n} of {total}: {q['q_hi']}"
    return f"Question {n} of {total}: {q['q_en']}"


def start_quiz(*, lang: str = "en") -> str:
    """Begin a new quiz with a random selection of questions."""
    global _quiz
    count = min(_QUIZ_ROUND, len(_QUIZ_QUESTIONS))
    order = random.sample(range(len(_QUIZ_QUESTIONS)), count)
    _quiz = _QuizGame(order=order)
    intro = (
        f"क्विज़ शुरू! मैं आपसे {count} सवाल पूछूँगी। "
        if lang == "hi" else
        f"Quiz time! I'll ask you {count} questions. "
    )
    return intro + _quiz_question_text(lang)


def play_quiz(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Grade the answer to the current question and move to the next one.

    Returns ``(ok, message)``. When the last question is answered or the player
    quits, the quiz ends and the final score is reported.
    """
    global _quiz
    if _quiz is None:  # defensive: caller should check is_quiz_active() first
        return False, start_quiz(lang=lang)

    if _has(text, _GAME_STOP_KWS):
        score, asked = _quiz.score, _quiz.idx
        _quiz = None
        if lang == "hi":
            return True, f"क्विज़ यहीं रोकते हैं। अब तक {asked} में से {score} सही।"
        return True, f"Stopping the quiz. You got {score} out of {asked} so far."

    q = _QUIZ_QUESTIONS[_quiz.order[_quiz.idx]]
    low = " " + " ".join(text.lower().split()) + " "
    correct = any(
        (f" {a} " in low) if a.isascii() else (a in text.lower())
        for a in q["accept"]
    )

    if correct:
        _quiz.score += 1
        feedback = "सही जवाब! " if lang == "hi" else "Correct! "
    else:
        feedback = (
            f"गलत। सही जवाब है {q['answer']}. " if lang == "hi"
            else f"Not quite. The answer is {q['answer']}. "
        )

    _quiz.idx += 1

    # More questions left? Ask the next one.
    if _quiz.idx < len(_quiz.order):
        return True, feedback + _quiz_question_text(lang)

    # Quiz finished — report the final score.
    score, total = _quiz.score, len(_quiz.order)
    _quiz = None
    if lang == "hi":
        return True, f"{feedback}क्विज़ खत्म! आपका स्कोर {total} में से {score} रहा।"
    return True, f"{feedback}Quiz over! Your final score is {score} out of {total}."


def is_ttt_active() -> bool:
    """True while a tic-tac-toe game is waiting for the player's move."""
    return _ttt is not None


def is_ttt_start(text: str) -> bool:
    """True if ``text`` asks to start tic-tac-toe."""
    return _has(text, _TTT_START_KWS)


def _ttt_board_str(board: list[str]) -> str:
    """Render the board, showing 1-9 for empty cells so the player can pick."""
    cells = [board[i] if board[i] != " " else str(i + 1) for i in range(9)]
    rows = [" " + " | ".join(cells[r:r + 3]) for r in (0, 3, 6)]
    return "\n---+---+---\n".join(rows)


def _ttt_winner(board: list[str]) -> str | None:
    """Return 'X' or 'O' if that mark owns a line, else None."""
    for a, b, c in _TTT_LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return None


def _ttt_sara_move(board: list[str]) -> int:
    """Pick Sara's ('O') move: win if possible, block, then centre/corner/any."""
    for mark in ("O", "X"):  # first try to win, then to block the player
        for a, b, c in _TTT_LINES:
            line = [board[a], board[b], board[c]]
            if line.count(mark) == 2 and line.count(" ") == 1:
                return (a, b, c)[line.index(" ")]
    if board[4] == " ":
        return 4
    for i in (0, 2, 6, 8):  # corners
        if board[i] == " ":
            return i
    return next(i for i in range(9) if board[i] == " ")


def start_tic_tac_toe(*, lang: str = "en") -> str:
    """Begin a new tic-tac-toe game with an empty board."""
    global _ttt
    _ttt = _TicTacToe(board=[" "] * 9)
    intro = (
        "टिक टैक टो! आप X हैं, मैं O। 1 से 9 तक कोई खाना चुनिए।\n"
        if lang == "hi" else
        "Tic-tac-toe! You are X, I am O. Pick a spot from 1 to 9.\n"
    )
    return intro + _ttt_board_str(_ttt.board)


def play_tic_tac_toe(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Play one turn: place the player's X, then Sara's O, and report the state.

    Returns ``(ok, message)``. When someone wins, the board fills (draw), or the
    player quits, the game ends.
    """
    global _ttt
    if _ttt is None:  # defensive: caller should check is_ttt_active() first
        return False, start_tic_tac_toe(lang=lang)

    board = _ttt.board

    if _has(text, _GAME_STOP_KWS):
        _ttt = None
        return True, (
            "ठीक है, खेल यहीं रोकते हैं।" if lang == "hi"
            else "Okay, stopping the game."
        )

    match = re.search(r"[1-9]", text)
    if not match:
        return False, (
            "1 से 9 तक कोई नंबर बताइए।" if lang == "hi"
            else "Please say a number from 1 to 9."
        )

    idx = int(match.group()) - 1
    if board[idx] != " ":
        return False, (
            "वह खाना पहले से भरा है, दूसरा चुनिए।" if lang == "hi"
            else "That spot is already taken, pick another."
        )

    # Player's move.
    board[idx] = "X"
    if _ttt_winner(board) == "X":
        result = _ttt_board_str(board)
        _ttt = None
        return True, (
            f"आप जीत गए! बधाई हो।\n{result}" if lang == "hi"
            else f"You win! Congratulations.\n{result}"
        )
    if " " not in board:
        result = _ttt_board_str(board)
        _ttt = None
        return True, (
            f"खेल बराबरी पर छूटा!\n{result}" if lang == "hi"
            else f"It's a draw!\n{result}"
        )

    # Sara's move.
    sara = _ttt_sara_move(board)
    board[sara] = "O"
    took = (
        f"मैंने {sara + 1} लिया।" if lang == "hi" else f"I took {sara + 1}."
    )
    if _ttt_winner(board) == "O":
        result = _ttt_board_str(board)
        _ttt = None
        return True, (
            f"{took} मैं जीत गई!\n{result}" if lang == "hi"
            else f"{took} I win!\n{result}"
        )
    if " " not in board:
        result = _ttt_board_str(board)
        _ttt = None
        return True, (
            f"{took} खेल बराबरी पर छूटा!\n{result}" if lang == "hi"
            else f"{took} It's a draw!\n{result}"
        )

    prompt = (
        f"{took} अब आपकी बारी।" if lang == "hi" else f"{took} Your turn."
    )
    return True, f"{prompt}\n{_ttt_board_str(board)}"
