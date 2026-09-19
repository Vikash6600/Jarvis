# Playbook: routine (recurring job)

This runs on a schedule. Each run: do the job described in the goal quickly with the fewest tool calls,
compare with previous results (given below) and call `finish` with a short result. Only mark the result
as important (report_progress important=true) when something changed or needs the user's attention.
Never ask the user questions in a routine; make a sensible assumption and note it.
