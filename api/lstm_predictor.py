import os
import numpy as np

_WEIGHTS = None

def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -500.0, 500.0)))

def _relu(x):
    return np.maximum(0.0, x)

def _get_weights():
    global _WEIGHTS
    if _WEIGHTS is None:
        npz_path = os.path.join(os.path.dirname(__file__), "model_lstm.npz")
        if not os.path.exists(npz_path):
            raise FileNotFoundError(f"Model file not found at: {npz_path}")
        data = np.load(npz_path)
        _WEIGHTS = {
            "w1": data["w1"],
            "u1": data["u1"],
            "b1": data["b1"],
            "w2": data["w2"],
            "u2": data["u2"],
            "b2": data["b2"],
            "wd": data["wd"],
            "bd": data["bd"],
            "scale": data["scale"],
            "min": data["min"],
            "features": list(data["features"]),
        }
    return _WEIGHTS

def _lstm_cell_step(x, h_prev, c_prev, W, U, b, units):
    z = np.dot(x, W) + np.dot(h_prev, U) + b
    z0 = z[0:units]
    z1 = z[units:2*units]
    z2 = z[2*units:3*units]
    z3 = z[3*units:4*units]

    i = _sigmoid(z0)
    f = _sigmoid(z1)
    c = f * c_prev + i * _relu(z2)
    o = _sigmoid(z3)
    h = o * _relu(c)
    return h, c

def predict_metar_lstm(sequence_10x4):
    """
    Run forward pass for a sequence of 10 METAR observations with 4 features:
    ['suhu_c', 'qnh_hpa', 'kec_angin_kt', 'dew_point_c']
    Returns a dict mapping each feature name to predicted float value.
    """
    weights = _get_weights()
    seq = np.asarray(sequence_10x4, dtype=np.float32)
    if seq.shape != (10, 4):
        raise ValueError(f"Expected input shape (10, 4), got {seq.shape}")

    # 1. Scale input
    scaled = seq * weights["scale"] + weights["min"]

    # 2. LSTM 1 (64 units, return_sequences=True)
    h1 = np.zeros(64, dtype=np.float32)
    c1 = np.zeros(64, dtype=np.float32)
    outputs1 = []
    for t in range(10):
        h1, c1 = _lstm_cell_step(scaled[t], h1, c1, weights["w1"], weights["u1"], weights["b1"], 64)
        outputs1.append(h1)

    # 3. LSTM 2 (32 units, return_sequences=False)
    h2 = np.zeros(32, dtype=np.float32)
    c2 = np.zeros(32, dtype=np.float32)
    for t in range(10):
        h2, c2 = _lstm_cell_step(outputs1[t], h2, c2, weights["w2"], weights["u2"], weights["b2"], 32)

    # 4. Dense layer (linear activation)
    y_scaled = np.dot(h2, weights["wd"]) + weights["bd"]

    # 5. Inverse scaling
    y_pred = (y_scaled - weights["min"]) / weights["scale"]

    results = {}
    for i, feat in enumerate(weights["features"]):
        results[str(feat)] = float(y_pred[i])
    return results


def predict_metar_multistep(sequence_10x4, steps=2):
    """
    Run autoregressive multi-step forecasting.
    Returns a list of dicts for each step (e.g., step 1 = +30m, step 2 = +1h).
    """
    seq = np.asarray(sequence_10x4, dtype=np.float32)
    weights = _get_weights()
    features = weights["features"]

    predictions = []
    current_seq = seq.copy()

    for _ in range(steps):
        pred_dict = predict_metar_lstm(current_seq)
        predictions.append(pred_dict)
        next_row = np.array([pred_dict[str(feat)] for feat in features], dtype=np.float32)
        current_seq = np.vstack([current_seq[1:], next_row])

    return predictions

