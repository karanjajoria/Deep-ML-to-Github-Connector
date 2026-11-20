"""Single Neuron (easy)
https://www.deep-ml.com/courses/neural-nets-to-gpt/learn?episode=ep1
status: solved
"""


import math

def single_neuron_model(features: list[list[float]], labels: list[int], weights: list[float], bias: float) -> tuple[list[float], float]:

    probabilities = []

    def sigmoid(x: float):
        return 1 / (1 + math.exp(-x))

    for feature in features:
        z = sum(f * w for f, w in zip(feature, weights)) + bias
        probabilities.append(sigmoid(z))
    

    mse = sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)

    probabilities = [round(p, 4) for p in probabilities]
    mse = round(mse, 4)

    return probabilities, mse
