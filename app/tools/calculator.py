# calculator.py — Safe math tool for pricing or calculations. Uses ast + allowed
# operators so the LLM can evaluate expressions without eval() of arbitrary code.

import ast
import operator
from langchain_core.tools import tool

ALLOWED = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _eval_node(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return ALLOWED[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        return ALLOWED[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"Unsupported: {type(node)}")


@tool
def calculate(expression: str) -> str:
    """
    Evaluate a mathematical expression safely. Supports +, -, *, /, **, %, parentheses.
    Example: "(2 + 3) * 4" returns "20". Use for pricing or any numeric calculation.
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval_node(tree.body)
        return str(result)
    except Exception as e:
        return f"Error: {e}"
