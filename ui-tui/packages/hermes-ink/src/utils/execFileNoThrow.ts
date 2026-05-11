import { spawn } from 'child_process'
type ExecFileOptions = {
  input?: string
  timeout?: number
  useCwd?: boolean
  env?: NodeJS.ProcessEnv
  /** Resolve as soon as the child *exits*, instead of waiting for its
   *  stdio streams to close. Use this for tools that fork a daemon and
   *  let the daemon inherit the parent's stdio (e.g. `wl-copy`): the
   *  child exits immediately, but `'close'` never fires because the
   *  daemon holds the pipes open. The caller must not depend on
   *  collecting stdout/stderr from that point on — only what arrived
   *  before exit is included in the resolved value. */
  resolveOnExit?: boolean
}

export function execFileNoThrow(
  file: string,
  args: string[],
  options: ExecFileOptions = {}
): Promise<{
  stdout: string
  stderr: string
  code: number
  error?: string
}> {
  return new Promise(resolve => {
    const child = spawn(file, args, {
      cwd: options.useCwd ? process.cwd() : undefined,
      env: options.env,
      stdio: 'pipe'
    })

    let stdout = ''
    let stderr = ''
    let timedOut = false
    let settled = false

    const settle = (code: number, error?: string) => {
      if (settled) {
        return
      }

      settled = true

      if (timer) {
        clearTimeout(timer)
      }

      resolve({ stdout, stderr, code, ...(error ? { error } : {}) })
    }

    const timer = options.timeout
      ? setTimeout(() => {
          timedOut = true
          child.kill('SIGTERM')

          // When resolving on exit, SIGTERM-ing a child that has already
          // exited is a no-op and `'exit'` won't fire again — settle here
          // so the promise doesn't leak. Safe under settled-guard.
          if (options.resolveOnExit) {
            settle(124)
          }
        }, options.timeout)
      : null

    child.stdout?.on('data', chunk => {
      stdout += String(chunk)
    })
    child.stderr?.on('data', chunk => {
      stderr += String(chunk)
    })
    child.on('error', error => {
      settle(1, String(error))
    })

    if (options.resolveOnExit) {
      // 'exit' fires when the child process itself exits — even if the
      // daemon it forked still holds the inherited stdio pipes open.
      child.on('exit', code => {
        settle(timedOut ? 124 : (code ?? 0))
      })
    } else {
      child.on('close', code => {
        settle(timedOut ? 124 : (code ?? 0))
      })
    }

    if (options.input) {
      child.stdin?.write(options.input)
    }

    child.stdin?.end()
  })
}
