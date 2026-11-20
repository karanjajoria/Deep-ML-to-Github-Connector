"""Matrix-Vector Dot Product (easy)
https://www.deep-ml.com/problems/1
status: unsolved
"""

def matrix_dot_vector(a: list[list[int|float]], b: list[int|float]) -> list[int|float]:
    ans = []
    for index in range(0,len(a)):
        for element in range(0,len(a)):
            temp = a[index][element] * b[element]
            ans.append(temp)
    pass
    return ans
