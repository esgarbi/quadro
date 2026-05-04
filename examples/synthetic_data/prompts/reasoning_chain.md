You are an expert at generating multi-hop reasoning training data
from factual passages. Given two related Wikipedia passages,
generate a question whose answer requires combining facts from
both passages, plus a step-by-step chain-of-thought reasoning
trace explaining how to arrive at the answer.

Requirements:
1. The question must require information from both passages -
   it cannot be answerable from either passage alone.
2. The chain-of-thought trace must be 3-5 steps, each step
   citing which passage the relevant fact came from (e.g.,
   "From passage 1: ..." and "From passage 2: ...").
3. The final answer must be a single concise statement (1-3
   sentences) that follows naturally from the reasoning chain.
4. Prefer questions that require connecting entities, comparing
   facts, or chaining causal relationships across the two
   passages.

Respond with a JSON object containing `instruction` (the
multi-hop question), `input` (the concatenated passage text
with clear separators), `reasoning` (the chain-of-thought
trace as a single string with newlines between steps), and
`output` (the final answer). This format follows the
Alpaca-style instruction-tuning convention with an extended
`reasoning` field.
