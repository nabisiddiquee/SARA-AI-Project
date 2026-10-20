import my_python_project.games as games


def teardown_function():
    # Make sure no game leaks between tests.
    games._active = None
    games._quiz = None
    games._ttt = None


def test_start_activates_game():
    assert not games.is_game_active()
    msg = games.start_number_game(lang="en")
    assert games.is_game_active()
    assert "number between" in msg.lower()


def test_higher_lower_and_correct():
    games._active = games._GuessGame(secret=42)

    ok, msg = games.play_number_game("10", lang="en")
    assert not ok and "higher" in msg.lower()
    assert games.is_game_active()

    ok, msg = games.play_number_game("90", lang="en")
    assert not ok and "lower" in msg.lower()

    ok, msg = games.play_number_game("42", lang="en")
    assert ok and "correct" in msg.lower()
    assert not games.is_game_active()  # game ends on a correct guess


def test_out_of_range_guess():
    games._active = games._GuessGame(secret=42)
    ok, msg = games.play_number_game("500", lang="en")
    assert not ok and "between" in msg.lower()
    assert games.is_game_active()


def test_stop_game():
    games._active = games._GuessGame(secret=7)
    ok, msg = games.play_number_game("stop game", lang="en")
    assert ok and "7" in msg
    assert not games.is_game_active()


def test_non_number_input():
    games._active = games._GuessGame(secret=7)
    ok, msg = games.play_number_game("hmm no idea", lang="en")
    assert not ok
    assert games.is_game_active()


def test_start_triggers():
    assert games.is_game_start("let's play the guessing game")
    assert games.is_game_start("number game khelo")
    assert not games.is_game_start("what's the weather")


def test_dice_and_coin():
    assert games.is_dice("roll the dice")
    ok, msg = games.roll_dice(lang="en")
    assert ok and "rolled a" in msg.lower()

    assert games.is_coin("flip a coin")
    ok, msg = games.flip_coin(lang="en")
    assert ok and ("heads" in msg.lower() or "tails" in msg.lower())


def test_hindi_replies():
    msg = games.start_number_game(lang="hi")
    assert "नंबर" in msg


def test_rps_triggers_and_move_parsing():
    assert games.is_rps("let's play rock paper scissors")
    assert games.is_rps("i choose rock")
    assert games.is_rps("kainchi")
    assert not games.is_rps("what's the weather")

    assert games._rps_move("i pick scissors") == "scissors"
    assert games._rps_move("rock") == "rock"
    assert games._rps_move("patthar") == "rock"
    assert games._rps_move("rps") is None  # game name only, no move


def test_rps_outcomes(monkeypatch):
    # Force Sara's move to make the outcome deterministic.
    monkeypatch.setattr(games.random, "choice", lambda opts: "scissors")

    ok, msg = games.play_rps("rock", lang="en")  # rock beats scissors
    assert ok and "you win" in msg.lower()

    ok, msg = games.play_rps("paper", lang="en")  # scissors beats paper
    assert ok and "i win" in msg.lower()

    ok, msg = games.play_rps("scissors", lang="en")  # tie
    assert ok and "tie" in msg.lower()


def test_rps_prompts_without_move():
    ok, msg = games.play_rps("rock paper scissors", lang="en")
    assert not ok and "move" in msg.lower()


def test_quiz_start_activates_and_asks():
    assert games.is_quiz_start("let's play a quiz")
    assert not games.is_quiz_start("what's the weather")

    assert not games.is_quiz_active()
    msg = games.start_quiz(lang="en")
    assert games.is_quiz_active()
    assert "question 1 of" in msg.lower()


def _force_quiz(order):
    games._quiz = games._QuizGame(order=list(order))


def test_quiz_scores_correct_and_wrong():
    # Q0 = capital of India (New Delhi), Q3 = days in a week (7).
    _force_quiz([0, 3])

    ok, msg = games.play_quiz("New Delhi", lang="en")
    assert ok and "correct" in msg.lower()
    assert "question 2 of 2" in msg.lower()  # advanced to next question
    assert games.is_quiz_active()

    ok, msg = games.play_quiz("nine", lang="en")  # wrong answer to "7"
    assert ok and "quiz over" in msg.lower()
    assert "1 out of 2" in msg.lower()  # only the first answer was right
    assert not games.is_quiz_active()  # quiz ends after the last question


def test_quiz_accepts_hindi_and_numeric():
    _force_quiz([3])  # "How many days in a week?" -> 7
    ok, msg = games.play_quiz("7", lang="en")
    assert ok and "correct" in msg.lower() and "quiz over" in msg.lower()


def test_quiz_stop():
    _force_quiz([0, 1, 2])
    games.play_quiz("New Delhi", lang="en")  # 1 correct, then quit
    ok, msg = games.play_quiz("stop game", lang="en")
    assert ok and "1 out of 1" in msg.lower()
    assert not games.is_quiz_active()


def test_ttt_start_and_triggers():
    assert games.is_ttt_start("let's play tic tac toe")
    assert games.is_ttt_start("tik tok")
    assert not games.is_ttt_start("what's the weather")

    assert not games.is_ttt_active()
    msg = games.start_tic_tac_toe(lang="en")
    assert games.is_ttt_active()
    assert "you are x" in msg.lower()


def test_ttt_winner_and_sara_move():
    # Sara ('O') should take the winning cell when two are already hers.
    board = ["O", "O", " ", "X", "X", " ", " ", " ", " "]
    assert games._ttt_sara_move(board) == 2

    # ...otherwise block the player's two-in-a-row.
    board = ["X", "X", " ", " ", "O", " ", " ", " ", " "]
    assert games._ttt_sara_move(board) == 2

    assert games._ttt_winner(["X", "X", "X", " ", " ", " ", " ", " ", " "]) == "X"
    assert games._ttt_winner([" "] * 9) is None


def test_ttt_player_wins():
    # Player one move from winning the top row (cells 1 and 2 taken).
    games._ttt = games._TicTacToe(board=["X", "X", " ", "O", "O", " ", " ", " ", " "])
    ok, msg = games.play_tic_tac_toe("3", lang="en")
    assert ok and "you win" in msg.lower()
    assert not games.is_ttt_active()


def test_ttt_taken_and_invalid_cells():
    games._ttt = games._TicTacToe(board=["X"] + [" "] * 8)
    ok, msg = games.play_tic_tac_toe("1", lang="en")  # already taken
    assert not ok and "taken" in msg.lower()
    assert games.is_ttt_active()

    ok, msg = games.play_tic_tac_toe("no idea", lang="en")  # no digit
    assert not ok and "1 to 9" in msg.lower()


def test_ttt_stop():
    games.start_tic_tac_toe(lang="en")
    ok, msg = games.play_tic_tac_toe("stop game", lang="en")
    assert ok and "stopping" in msg.lower()
    assert not games.is_ttt_active()
