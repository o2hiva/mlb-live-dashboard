"""
First-inning run-probability predictor.

This is intentionally pluggable:

  1. If a trained model artifact exists at MODEL_PATH (a pickled
     scikit-learn / XGBoost model, matching the pipeline you already
     built - logistic regression baseline or XGBoost + isotonic
     calibration), it's loaded and used.
  2. Otherwise, falls back to a transparent placeholder formula so the
     whole app runs end-to-end today. Replace this the moment your
     trained model + feature pipeline are ready to export.

To wire in your real model:
  - Export it with `joblib.dump(model, "model.pkl")`
  - Also export whatever feature-building function your pipeline uses,
    or reimplement it in `build_features()` below using the same
    starting-pitcher / top-of-lineup / park-factor / weather inputs
    you specified.
"""
import os
import joblib

MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")

_model = None
if os.path.exists(MODEL_PATH):
    _model = joblib.load(MODEL_PATH)


def build_features(game_info: dict) -> dict:
    """
    Placeholder feature builder. Swap this for your real feature
    engineering (starting pitcher stats, top-of-lineup OBP/SLG,
    park factor, weather, etc.) once available.
    """
    return {
        "home_probable_pitcher": game_info.get("home_probable_pitcher"),
        "away_probable_pitcher": game_info.get("away_probable_pitcher"),
    }


def predict_first_inning_run_prob(game_info: dict) -> tuple[float, str]:
    """
    Returns (probability, model_version).
    """
    features = build_features(game_info)

    if _model is not None:
        # Adjust this to match your real model's expected input shape.
        proba = float(_model.predict_proba([list(features.values())])[0][1])
        return proba, "trained-model-v1"

    # --- Placeholder formula ---
    # MLB league-average probability that at least one run scores in the
    # 1st inning sits roughly in the 45-50% range historically. This is
    # a flat baseline, NOT a real model - it exists so the pipeline is
    # runnable before your trained model is wired in.
    baseline = 0.47
    return baseline, "placeholder-v0"
