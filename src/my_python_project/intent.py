"""Sara's trainable intent model.

A small multinomial logistic-regression classifier over word and character
n-grams, written in plain Python (no numpy/scikit-learn) so it trains in a few
seconds on any laptop CPU. It answers one question for every command: *what
does the user want?* (open an app, close an app, the weather, just chatting...).

Training data comes from three places:

1. the built-in examples in :mod:`intent_data`,
2. the apps installed on *this* PC (Start Menu shortcuts become "open X" /
   "X band karo" examples), so the model knows your own software, and
3. your own phrases in ``my_training_data.json`` in the project folder.

Train with ``python -m my_python_project train``. The trained weights are saved
to ``models/intent_model.json``; if that file is missing the model trains itself
on first use.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

from .intent_data import INTENT_EXAMPLES

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = _PROJECT_ROOT / "models" / "intent_model.json"
USER_DATA_PATH = _PROJECT_ROOT / "my_training_data.json"

# Intents that run something on the PC. When the model is confident the input
# is plain conversation ("chat"), these keyword branches are skipped.
CHAT = "chat"

_EPOCHS = 25
_LEARNING_RATE = 0.5
_L2 = 1e-4
_MAX_SYSTEM_APPS = 150


@dataclass(frozen=True)
class Prediction:
    intent: str
    confidence: float


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\d+", " 0 ", text)  # all numbers look alike
    text = re.sub(r"[^\w\s+]", " ", text)
    return " ".join(text.split())


def features(text: str) -> list[str]:
    """Word unigrams/bigrams plus character 3-4-grams (robust to typos)."""
    words = _normalize(text).split()
    feats = ["bias"]
    feats += [f"w:{w}" for w in words]
    feats += [f"b:{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    for w in words:
        padded = f" {w} "
        for n in (3, 4):
            feats += [f"c:{padded[i:i + n]}" for i in range(len(padded) - n + 1)]
    return feats


def _softmax(scores: list[float]) -> list[float]:
    top = max(scores)
    exps = [math.exp(s - top) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


class IntentModel:
    def __init__(self, labels: list[str], weights: dict[str, list[float]]):
        self.labels = labels
        self.weights = weights

    def probabilities(self, text: str) -> list[float]:
        scores = [0.0] * len(self.labels)
        for f in features(text):
            w = self.weights.get(f)
            if w is not None:
                for i, v in enumerate(w):
                    scores[i] += v
        return _softmax(scores)

    def predict(self, text: str) -> Prediction:
        probs = self.probabilities(text)
        best = max(range(len(probs)), key=probs.__getitem__)
        return Prediction(self.labels[best], probs[best])

    @classmethod
    def train(cls, examples: list[tuple[str, str]], *, epochs: int = _EPOCHS,
              seed: int = 13) -> IntentModel:
        """Fit the classifier with stochastic gradient descent."""
        labels = sorted({label for _, label in examples})
        index = {label: i for i, label in enumerate(labels)}
        k = len(labels)
        data = [(features(text), index[label]) for text, label in examples]
        weights: dict[str, list[float]] = {}
        rng = random.Random(seed)
        for epoch in range(epochs):
            rng.shuffle(data)
            lr = _LEARNING_RATE / (1 + epoch * 0.2)
            for feats, y in data:
                scores = [0.0] * k
                for f in feats:
                    w = weights.get(f)
                    if w is not None:
                        for i in range(k):
                            scores[i] += w[i]
                probs = _softmax(scores)
                probs[y] -= 1.0  # gradient of cross-entropy w.r.t. the scores
                step = [lr * g for g in probs]
                for f in feats:
                    w = weights.setdefault(f, [0.0] * k)
                    for i in range(k):
                        w[i] -= step[i] + lr * _L2 * w[i]
        return cls(labels, weights)

    def save(self, path: Path = MODEL_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rounded = {f: [round(v, 4) for v in w] for f, w in self.weights.items()}
        path.write_text(
            json.dumps({"labels": self.labels, "weights": rounded}, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> IntentModel:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["labels"], data["weights"])


# --- Training data -----------------------------------------------------------

def load_user_examples(path: Path = USER_DATA_PATH) -> list[tuple[str, str]]:
    """Your own phrases: ``{"intent": ["example", ...]}`` in my_training_data.json."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [(text, intent) for intent, texts in data.items() for text in texts]


def add_user_example(text: str, intent: str, path: Path = USER_DATA_PATH) -> None:
    data: dict[str, list[str]] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    texts = data.setdefault(intent, [])
    if text not in texts:
        texts.append(text)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def system_app_examples() -> list[tuple[str, str]]:
    """Open/close examples for the apps installed on this PC."""
    try:
        from .apps import _iter_shortcuts
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    for _path, stem in _iter_shortcuts():
        name = _normalize(stem)
        # Skip uninstallers, readmes, help links, and very long names.
        if (not name or len(name.split()) > 4
                or re.search(r"uninstall|readme|help|documentation|website|license", name)):
            continue
        if name not in names:
            names.append(name)
    rng = random.Random(7)
    rng.shuffle(names)
    out: list[tuple[str, str]] = []
    for name in names[:_MAX_SYSTEM_APPS]:
        out.append((rng.choice(["open {}", "{} kholo", "launch {}", "{} open karo"])
                    .format(name), "open_app"))
        out.append((rng.choice(["close {}", "{} band karo", "quit {}"])
                    .format(name), "close_app"))
    return out


def training_examples(*, include_system: bool = True) -> list[tuple[str, str]]:
    examples = [(t, intent) for intent, texts in INTENT_EXAMPLES.items() for t in texts]
    if include_system:
        examples += system_app_examples()
    return examples + load_user_examples()


def evaluate(examples: list[tuple[str, str]], folds: int = 5) -> float:
    """K-fold cross-validated accuracy (how well it handles unseen phrases)."""
    data = list(examples)
    random.Random(1).shuffle(data)
    correct = 0
    for k in range(folds):
        test = data[k::folds]
        train = [e for i, e in enumerate(data) if i % folds != k]
        model = IntentModel.train(train)
        correct += sum(model.predict(t).intent == y for t, y in test)
    return correct / len(data)


def train_and_save(*, include_system: bool = True) -> tuple[IntentModel, int]:
    examples = training_examples(include_system=include_system)
    model = IntentModel.train(examples)
    model.save()
    global _model
    _model = model
    return model, len(examples)


# --- Runtime -----------------------------------------------------------------

_model: IntentModel | None = None


def get_model() -> IntentModel | None:
    """The trained model (loads it, or trains once if it's missing)."""
    global _model
    if _model is None:
        if os.environ.get("SARA_INTENT_MODEL", "1") == "0":
            return None
        try:
            _model = IntentModel.load() if MODEL_PATH.is_file() else train_and_save()[0]
        except Exception:  # noqa: BLE001 - never let the model break Sara
            return None
    return _model


def predict(text: str) -> Prediction | None:
    model = get_model()
    return model.predict(text) if model else None


def is_chat(text: str, threshold: float = 0.6) -> bool:
    """True when the model is confident ``text`` is conversation, not a command."""
    pred = predict(text)
    return bool(pred and pred.intent == CHAT and pred.confidence >= threshold)
