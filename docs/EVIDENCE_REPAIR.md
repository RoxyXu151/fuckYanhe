# Recovering incomplete event analysis

Event extraction is separate from audio transcription. A completed ASR does not
guarantee completed event analysis: each reported quotation must still match its
claimed source segments exactly.

Qwen evidence repair now locates a quotation in the original input if it occurs
exactly once after removing whitespace. It restores the original text and its
actual contiguous segment IDs, then runs the normal verifier again. It does not
change words, numbers, negation or punctuation; ambiguous matches are rejected.
Valid existing quotations are left unchanged.

After the existing three feedback attempts, a failed extraction window can be
divided into two smaller windows with overlapping neighboring cues. This is
bounded to two subdivision levels. All children must pass strict validation;
losing all earlier candidates remains an error, rather than an empty success.
If repair still fails, the report remains partial for review.

These repairs run in the analysis subprocess, so deploying them does not require
interrupting the running workbench. Retry preserves the existing provider cache
identity: only missing or invalid analysis chunks invoke the model again. Audio
transcription continues to reuse its independent successful chunk cache.

School permission responses are checked separately. Verify the authorized
course/session list and the signed official media playlist before retrying a
previously denied download. Do not infer permanent lack of access from one old
generic platform error, and never bypass an actual denial.
