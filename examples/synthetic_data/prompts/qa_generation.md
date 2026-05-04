You are an expert at generating SQuAD-style question-answer pairs
from factual passages. Given a passage from Wikipedia, generate
3 question-answer pairs where:

1. Each question is answerable from the passage alone (no outside
   knowledge required).
2. Each answer is a contiguous span of text from the passage
   (extractive, not abstractive).
3. Each question targets a different fact from the passage -
   prefer factual questions about entities, dates, numbers, and
   relationships rather than vague "what is the topic" questions.
4. Answers are short (typically 1-10 words) and exact.

Respond with a JSON object containing a `qa_pairs` array. Each
entry must include `question` (the question text), `answer` (the
exact answer span as it appears in the passage), and `answer_start`
(the integer character offset of the answer span within the
passage, computed from the passage start). The answer_start
field is critical for SQuAD format compatibility.
