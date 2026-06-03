import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { AlertCircle, Check, FileText, Globe, Loader2, Monitor } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import { CONTROL_TEXT } from './constants'
import { EmptyState, ListRow, LoadingState, Pill, SettingsContent } from './primitives'

type Mode = 'local' | 'remote'
type RemoteSource = 'manual' | 'ssh'

interface GatewaySettingsState {
  envOverride: boolean
  mode: Mode
  remoteSource: RemoteSource
  remoteTokenPreview: string | null
  remoteTokenSet: boolean
  remoteUrl: string
  sshCommand: string
  sshHost: string
  sshRemotePort: number
}

const DEFAULT_SSH_PORT = 0

function defaultSshCommand() {
  return 'hermes dashboard --no-open --tui --host 127.0.0.1 --port {port} --skip-build'
}

const EMPTY_STATE: GatewaySettingsState = {
  envOverride: false,
  mode: 'local',
  remoteSource: 'manual',
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: '',
  sshCommand: defaultSshCommand(),
  sshHost: '',
  sshRemotePort: DEFAULT_SSH_PORT
}

function ModeCard({
  active,
  description,
  disabled,
  icon: Icon,
  onSelect,
  title
}: {
  active: boolean
  description: string
  disabled?: boolean
  icon: typeof Monitor
  onSelect: () => void
  title: string
}) {
  return (
    <button
      className={cn(
        'rounded-xl border p-3 text-left transition',
        active
          ? 'border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary)'
          : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) hover:bg-(--chrome-action-hover)',
        disabled && 'cursor-not-allowed opacity-50'
      )}
      disabled={disabled}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-center gap-2 text-[length:var(--conversation-text-font-size)] font-medium">
        <Icon className="size-4 text-muted-foreground" />
        <span>{title}</span>
        {active ? <Check className="ml-auto size-4 text-primary" /> : null}
      </div>
      <p className="mt-1.5 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {description}
      </p>
    </button>
  )
}

export function GatewaySettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [state, setState] = useState<GatewaySettingsState>(EMPTY_STATE)
  const [remoteToken, setRemoteToken] = useState('')
  const [lastTest, setLastTest] = useState<null | string>(null)

  useEffect(() => {
    let cancelled = false
    const desktop = window.hermesDesktop

    if (!desktop?.getConnectionConfig) {
      setLoading(false)

      return () => void (cancelled = true)
    }

    desktop
      .getConnectionConfig()
      .then(config => {
        if (cancelled) {
          return
        }

        setState({ ...EMPTY_STATE, ...config })
      })
      .catch(err => notifyError(err, 'Gateway settings failed to load'))
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => void (cancelled = true)
  }, [])

  const canUseManualRemote = useMemo(
    () => Boolean(state.remoteUrl.trim()) && (Boolean(remoteToken.trim()) || state.remoteTokenSet),
    [remoteToken, state.remoteTokenSet, state.remoteUrl]
  )

  const canUseSshRemote = useMemo(
    () => Boolean(state.sshHost.trim()) && Number.isInteger(Number(state.sshRemotePort)) && Number(state.sshRemotePort) >= 0,
    [state.sshHost, state.sshRemotePort]
  )

  const canUseRemote = state.remoteSource === 'ssh' ? canUseSshRemote : canUseManualRemote

  const payload = () => ({
    mode: state.mode,
    remoteSource: state.remoteSource,
    remoteToken: remoteToken.trim() || undefined,
    remoteUrl: state.remoteUrl.trim(),
    sshCommand: state.sshCommand.trim() || defaultSshCommand(),
    sshHost: state.sshHost.trim(),
    sshRemotePort: Number(state.sshRemotePort) || DEFAULT_SSH_PORT
  })

  const save = async (apply: boolean) => {
    if (state.mode === 'remote' && !canUseRemote) {
      notify({
        kind: 'warning',
        title: 'Remote gateway incomplete',
        message:
          state.remoteSource === 'ssh'
            ? 'Enter an SSH host alias before switching to remote SSH.'
            : 'Enter a remote URL and session token before switching to remote.'
      })

      return
    }

    setSaving(true)

    try {
      const next = apply
        ? await window.hermesDesktop.applyConnectionConfig(payload())
        : await window.hermesDesktop.saveConnectionConfig(payload())

      setState({ ...EMPTY_STATE, ...next })
      setRemoteToken('')
      notify({
        kind: 'success',
        title: apply ? 'Gateway connection restarting' : 'Gateway settings saved',
        message: apply ? 'Hermes Desktop will reconnect using the saved settings.' : 'Saved for the next restart.'
      })
    } catch (err) {
      notifyError(err, apply ? 'Could not apply gateway settings' : 'Could not save gateway settings')
    } finally {
      setSaving(false)
    }
  }

  const testRemote = async () => {
    if (!canUseRemote) {
      notify({
        kind: 'warning',
        title: 'Remote gateway incomplete',
        message:
          state.remoteSource === 'ssh'
            ? 'Enter an SSH host alias before testing.'
            : 'Enter a remote URL and session token before testing.'
      })

      return
    }

    setTesting(true)
    setLastTest(null)

    try {
      const result = await window.hermesDesktop.testConnectionConfig(
        state.remoteSource === 'ssh'
          ? { mode: 'remote', remoteSource: 'ssh', sshCommand: payload().sshCommand, sshHost: payload().sshHost, sshRemotePort: payload().sshRemotePort }
          : { mode: 'remote', remoteSource: 'manual', remoteToken: remoteToken.trim() || undefined, remoteUrl: state.remoteUrl.trim() }
      )

      const message = `Connected to ${result.baseUrl}${result.version ? ` · Hermes ${result.version}` : ''}`
      setLastTest(message)
      notify({ kind: 'success', title: 'Remote gateway reachable', message })
    } catch (err) {
      notifyError(err, 'Remote gateway test failed')
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return <LoadingState label="Loading gateway settings..." />
  }

  if (!window.hermesDesktop?.getConnectionConfig) {
    return (
      <EmptyState
        description="The desktop IPC bridge does not expose gateway settings."
        title="Gateway settings unavailable"
      />
    )
  }

  return (
    <SettingsContent>
      <div className="mb-5">
        <div className="flex items-center gap-2 text-[length:var(--conversation-text-font-size)] font-medium">
          <Globe className="size-4 text-muted-foreground" />
          Gateway Connection
          {state.envOverride ? <Pill tone="primary">env override</Pill> : null}
        </div>
        <p className="mt-2 max-w-2xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
          Hermes Desktop starts its own local gateway by default. Use a remote gateway when you want this app to control
          an already-running Hermes backend or bootstrap one over SSH.
        </p>
      </div>

      {state.envOverride ? (
        <div className="mb-5 flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[length:var(--conversation-caption-font-size)] text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <div>
            <div className="font-medium">Environment variables are controlling this desktop session.</div>
            <div className="mt-1 leading-5">
              Unset <code>HERMES_DESKTOP_REMOTE_URL</code> and <code>HERMES_DESKTOP_REMOTE_TOKEN</code> to use the saved
              setting below.
            </div>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2">
        <ModeCard
          active={state.mode === 'local'}
          description="Start a private Hermes backend on localhost. This is the default and works offline."
          disabled={state.envOverride}
          icon={Monitor}
          onSelect={() => setState(current => ({ ...current, mode: 'local' }))}
          title="Local gateway"
        />
        <ModeCard
          active={state.mode === 'remote'}
          description="Connect this desktop shell to a remote Hermes backend manually or over SSH."
          disabled={state.envOverride}
          icon={Globe}
          onSelect={() => setState(current => ({ ...current, mode: 'remote' }))}
          title="Remote gateway"
        />
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <ModeCard
          active={state.remoteSource === 'manual'}
          description="Paste a remote dashboard URL and its session token."
          disabled={state.envOverride || state.mode !== 'remote'}
          icon={Globe}
          onSelect={() => setState(current => ({ ...current, mode: 'remote', remoteSource: 'manual' }))}
          title="Manual URL + token"
        />
        <ModeCard
          active={state.remoteSource === 'ssh'}
          description="Use your local SSH config to start and forward the remote dashboard automatically."
          disabled={state.envOverride || state.mode !== 'remote'}
          icon={Monitor}
          onSelect={() => setState(current => ({ ...current, mode: 'remote', remoteSource: 'ssh' }))}
          title="SSH host"
        />
      </div>

      {state.remoteSource === 'ssh' ? (
        <div className="mt-5 divide-y divide-border/40">
          <ListRow
            action={
              <Input
                className={cn('h-8 font-mono', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => setState(current => ({ ...current, sshHost: event.target.value }))}
                placeholder="oracle-hermes"
                value={state.sshHost}
              />
            }
            description="SSH host alias or user@host. Hermes uses your local ~/.ssh/config, ssh-agent, keys, and ProxyJump settings."
            title="SSH host"
          />
          <ListRow
            action={
              <Input
                className={cn('h-8 font-mono', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => {
                  const nextPort = Number(event.target.value) || DEFAULT_SSH_PORT
                  setState(current => ({
                    ...current,
                    sshCommand: current.sshCommand === defaultSshCommand() ? defaultSshCommand() : current.sshCommand,
                    sshRemotePort: nextPort
                  }))
                }}
                type="number"
                value={state.sshRemotePort}
              />
            }
            description="Port on the remote host where the dashboard should listen on 127.0.0.1. Use 0 for auto, which avoids conflicts across multiple desktop windows."
            title="Remote dashboard port"
          />
          <ListRow
            action={
              <Input
                className={cn('h-8 font-mono', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => setState(current => ({ ...current, sshCommand: event.target.value }))}
                value={state.sshCommand}
              />
            }
            description="Command run on the remote host after SSH connects. Desktop injects the session token and replaces {port} with the chosen remote port."
            title="Remote command"
          />
        </div>
      ) : (
        <div className="mt-5 divide-y divide-border/40">
          <ListRow
            action={
              <Input
                className={cn('h-8', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => setState(current => ({ ...current, remoteUrl: event.target.value }))}
                placeholder="https://gateway.example.com/hermes"
                value={state.remoteUrl}
              />
            }
            description="Base URL for the remote dashboard backend. Path prefixes are supported, for example /hermes."
            title="Remote URL"
          />
          <ListRow
            action={
              <Input
                autoComplete="off"
                className={cn('h-8 font-mono', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => setRemoteToken(event.target.value)}
                placeholder={state.remoteTokenSet ? `Existing token ${state.remoteTokenPreview ?? 'saved'}` : 'Paste session token'}
                type="password"
                value={remoteToken}
              />
            }
            description="The dashboard session token used for REST and WebSocket access. Leave blank to keep the saved token."
            title="Session token"
          />
        </div>
      )}

      {lastTest ? <div className="mt-4 text-xs text-primary">{lastTest}</div> : null}

      <div className="mt-6 flex flex-wrap justify-end gap-3">
        <Button
          disabled={state.envOverride || testing || !canUseRemote || state.mode !== 'remote'}
          onClick={() => void testRemote()}
          variant="outline"
        >
          {testing ? <Loader2 className="size-4 animate-spin" /> : null}
          {state.remoteSource === 'ssh' ? 'Test SSH host' : 'Test remote'}
        </Button>
        <Button disabled={state.envOverride || saving} onClick={() => void save(false)} variant="outline">
          Save for next restart
        </Button>
        <Button disabled={state.envOverride || saving} onClick={() => void save(true)}>
          {saving ? <Loader2 className="size-4 animate-spin" /> : null}
          Save and reconnect
        </Button>
      </div>

      <div className="mt-6 divide-y divide-border/40">
        <ListRow
          action={
            <Button onClick={() => void window.hermesDesktop?.revealLogs()} variant="outline">
              <FileText className="size-4" />
              Open logs
            </Button>
          }
          description="Reveal desktop.log in your file manager — useful when the gateway fails to start."
          title="Diagnostics"
        />
      </div>
    </SettingsContent>
  )
}
