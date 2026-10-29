# Native execution: proposed contract (not implemented)

The Unix-socket bridge is read-only. `/execute_tool` stays disabled. This proposal needs agreement before execution is enabled.

## Why execution is separate

The old execution route was disabled because injecting into a live interactive PTY and capturing output with markers can race with terminal input. Current shell hooks detect prompt readiness, but `_track_pty_controls` discards OSC 133 completion parameters, and the fish hook only identifies a prompt. The existing `shell_state` does not track actual command execution.

Running a separate subprocess would be simpler, but would not run in the user's shell: changes to directories, environment, functions and shell options would not reliably carry over. It is not a replacement for this capability.

## Proposed minimum

Implement shell-native hooks, initially for fish only, with a separate inherited control channel for readiness, command start and completion status. Do not infer completion from terminal output. Unsupported shells fail closed; bash/zsh support is a separate increment.

Expose a new command resource over the existing private Unix socket, not `/execute_tool`:

- `POST /commands`: accept a bounded UTF-8 command and return a server-generated command ID immediately. Reserve the shell and submit atomically under the same input lock used for terminal and voice writes. Return 409 when running, input is dirty, a local foreground job owns the terminal, or readiness is unknown. Never queue or retry a submission automatically.
- `GET /commands/<id>`: report accepted/running/completed/interrupted/unknown, bounded output, truncation flag and nullable exit status. Keep a bounded history; return 404 for expired IDs. Terminal output is merged stdout/stderr, not separate streams.
- `POST /commands/<id>/cancel`: request interrupt of only that active command. Acknowledge the request, not successful termination. Completion still requires a shell integration event. Do not signal a shell process group or an unrelated foreground job.

Local input and voice injection must not interleave with accepted command submission. While a native command owns the shell, reject voice commands and reserve local input except an explicitly defined interrupt action. This changes interactive behavior and is a product decision, not an incidental implementation detail.

Client disconnects and request deadlines never stop accepted commands. Clients reconnect and query the ID; a response lost during submission means acceptance is unknown and must not trigger automatic resubmission. There is no automatic command timeout in the initial contract. Shell exit marks any active command interrupted/unknown rather than inventing an exit status. Background descendants are not guaranteed to stop when the foreground command completes or is interrupted.

## Safety questions that gate implementation

1. Accept an initial fish-only capability, with other shells returning unavailable?
2. Accept reserving terminal input while a native command is active, rather than mixing remote execution with interactive typing?
3. Accept interrupt-request cancellation without a guarantee that processes or descendants have exited?

These must be settled before choosing the shell-hook transport and implementing command acceptance. Shell integration is a correctness mechanism, not a security boundary against malicious commands running as the same user.

## Required evidence

Use real disposable PTYs plus HTTP-over-socket tests, without KAI: shell startup and empty prompt, dirty prompt rejection, local/voice submission races, simultaneous HTTP submissions (one accepted), two instances with distinct IDs/output, nonzero exit status, long-running commands, output caps, split hook events, disconnect/reconnect, cancellation races with completion/new commands, command ignoring SIGINT, shell exit, and restart. Run in both Kitty and tmux before enabling the capability. Later, verify KAI integration separately.
