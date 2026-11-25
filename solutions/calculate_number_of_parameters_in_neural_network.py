"""Calculate Number of Parameters in Neural Network (easy)
https://www.deep-ml.com/problems/371?from=Neural%20Nets%20to%20GPT
status: solved
"""

def calculate_parameters(layers: list[dict]) -> int:
    """
    Calculate the total number of trainable parameters in a neural network.

    Args:
        layers: List of dictionaries, each describing a layer.

    Returns:
        Total number of trainable parameters (int).
    """
    # Formula (For Dense) => input*output + bias {For each layer}
    # therefore ([784(input)*128(output)] + 128(bias)) + ([128(input)*10(output) + 10(bias)]) == 101770

    total_params = 0

    for element in layers:
        if element["type"] == "dense":
            if element["bias"] == True:
                temp = element["input_size"] * element["output_size"] + element["output_size"]
        total_params += temp
    pass

    return total_params
