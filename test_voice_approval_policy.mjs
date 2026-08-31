import assert from 'node:assert/strict'
import { apply } from './.dsh_home/profiles/headless/plugins/voice-approval/index.mjs'

let handler
const ctx = {
  on(name, fn) {
    assert.equal(name, 'approval/request')
    handler = fn
  },
  logger: { warn() {} },
}

const oldFetch = globalThis.fetch
const oldEnv = {
  task: process.env.ASSISTANT_TASK_ID,
  epoch: process.env.ASSISTANT_EXECUTOR_EPOCH,
  token: process.env.ASSISTANT_CANCEL_TOKEN,
}

try {
  process.env.ASSISTANT_TASK_ID = 'task-voice'
  process.env.ASSISTANT_EXECUTOR_EPOCH = '7'
  process.env.ASSISTANT_CANCEL_TOKEN = 'c'.repeat(32)
  apply(ctx, {
    url: 'http://127.0.0.1.invalid/confirm',
    tokenPath: 'persona.yaml',
    authorityPath: '.test-authority-does-not-exist',
  })

  globalThis.fetch = async (_url, options) => {
    const envelope = JSON.parse(options.body)
    return { ok: true, json: async () => ({ ...envelope, outcome: 'allowed-once' }) }
  }
  assert.equal(await handler({
    id: 'call-ok', toolName: 'ui_click', input: { x: 1, y: 2 },
  }), 'allowed-once')

  process.env.ASSISTANT_EXECUTOR_EPOCH = '7'
  globalThis.fetch = async (_url, options) => {
    const envelope = JSON.parse(options.body)
    process.env.ASSISTANT_EXECUTOR_EPOCH = '8'
    return { ok: true, json: async () => ({ ...envelope, outcome: 'allowed-once' }) }
  }
  assert.equal(await handler({
    id: 'call-stale', toolName: 'ui_click', input: { x: 3, y: 4 },
  }), 'unavailable')

  process.env.ASSISTANT_EXECUTOR_EPOCH = '7'
  globalThis.fetch = async (_url, options) => {
    const envelope = JSON.parse(options.body)
    return { ok: true, json: async () => ({ ...envelope, approved: true }) }
  }
  assert.equal(await handler({
    id: 'call-legacy', toolName: 'ui_click', input: { x: 5, y: 6 },
  }), 'unavailable')

  globalThis.fetch = async () => ({ ok: false, status: 503 })
  assert.equal(await handler({
    id: 'call-http', toolName: 'ui_click', input: { x: 7, y: 8 },
  }), 'unavailable')
} finally {
  globalThis.fetch = oldFetch
  if (oldEnv.task === undefined) delete process.env.ASSISTANT_TASK_ID
  else process.env.ASSISTANT_TASK_ID = oldEnv.task
  if (oldEnv.epoch === undefined) delete process.env.ASSISTANT_EXECUTOR_EPOCH
  else process.env.ASSISTANT_EXECUTOR_EPOCH = oldEnv.epoch
  if (oldEnv.token === undefined) delete process.env.ASSISTANT_CANCEL_TOKEN
  else process.env.ASSISTANT_CANCEL_TOKEN = oldEnv.token
}

console.log('VOICE_APPROVAL_POLICY_TEST PASS')
