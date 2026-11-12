<template>
  <section class="page" data-module="notify">
    <header class="page-head">
      <div>
        <h2>通知分发管理</h2>
        <p class="page-desc">
          投递结果按未发送 / 等待确认 / 对端拒收 / 已确认送达四态管理；连接超时只算结果不确定，不会自动重试。
          本地接收端用于演示拒收、超时与迟到回执，不接真实通知服务。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openEventDialog">登记事件并投递</button>
        <button class="btn" type="button" @click="resumeAll">恢复续发</button>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- ============================ 投递台账 ============================ -->
    <div v-if="activeTab === 'deliveries'">
      <div class="stat-row">
        <article v-for="item in statCards" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value" :class="item.cls">{{ item.value }}</strong>
        </article>
      </div>

      <div class="receiver-banner" :class="receiverOnline ? 'online' : 'offline'">
        <span>
          本地接收端：<strong>{{ receiverOnline ? '在线' : '离线' }}</strong>
          · 当前模式：{{ receiverStatus.mode_label || '—' }}
          · 已登记渠道 {{ receiverStatus.channels?.length || 0 }} 个
        </span>
        <button class="link" type="button" @click="switchTab('receiver')">前往接收端控制台 →</button>
      </div>

      <form class="filter-bar" @submit.prevent="reloadDeliveries">
        <label class="filter-item">
          <span>投递状态</span>
          <select v-model="statusFilter">
            <option value="">全部状态</option>
            <option v-for="(label, key) in statusLabels" :key="key" :value="key">{{ label }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>关键词</span>
          <input v-model="keyword" placeholder="投递编号 / 事件摘要 / 渠道" />
        </label>
        <label class="filter-check">
          <input v-model="legacyOnly" type="checkbox" />
          <span>只看历史待核对</span>
        </label>
        <button class="btn" type="submit">查询</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>投递编号</th>
            <th>原始检测事件</th>
            <th>渠道 / 接收方</th>
            <th>投递状态</th>
            <th>尝试</th>
            <th>证据</th>
            <th>阻断原因与下一步</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in deliveries" :key="row.id" :class="{ legacy: row.legacy }">
            <td>
              {{ row.delivery_no }}
              <span v-if="row.legacy" class="tag tag-warn">历史无标识</span>
            </td>
            <td>
              <div class="cell-primary">{{ row.event_no }}</div>
              <div class="cell-sub">{{ row['事件摘要'] }}</div>
            </td>
            <td>
              <div class="cell-primary">{{ row.channel }}</div>
              <div class="cell-sub">{{ row.recipient }}</div>
            </td>
            <td><span class="badge" :class="statusClass(row.status)">{{ row.status_label }}</span></td>
            <td>{{ row.attempt }}</td>
            <td>{{ evidenceLabel(row.evidence_level) }}</td>
            <td>
              <div v-if="row.block_reason" class="block-reason">⛔ {{ row.block_reason_label }}</div>
              <div v-else class="cell-sub">无阻断</div>
              <div class="next-actions">可执行：{{ (row.next_actions && row.next_actions.length) ? row.next_actions.join('、') : '无' }}</div>
            </td>
            <td class="row-actions wrap">
              <button
                v-for="action in row.next_actions"
                :key="action"
                class="link"
                type="button"
                @click="handleAction(action, row)"
              >
                {{ action }}
              </button>
              <button class="link" type="button" @click="openDetail(row)">明细/回执</button>
            </td>
          </tr>
          <tr v-if="!deliveries.length">
            <td colspan="8" class="empty-state">暂无投递记录，可先登记异常事件并发起投递</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============================ 检测事件 ============================ -->
    <div v-if="activeTab === 'events'">
      <p class="page-desc">
        异常检测事件的通知消费结论与通知分发模块实时同步：只要还有「等待确认」的在途投递，原事件就不会被标记为已处理。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>事件编号</th>
            <th>来源规则 / 指标</th>
            <th>级别</th>
            <th>事件摘要</th>
            <th>通知消费结论</th>
            <th>处理留痕</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="event.id">
            <td>{{ event.event_no }}</td>
            <td>
              <div class="cell-primary">{{ event.source_rule || '—' }}</div>
              <div class="cell-sub">{{ event.metric }}</div>
            </td>
            <td>{{ event.level }}</td>
            <td>{{ event.summary }}</td>
            <td><span class="badge" :class="consumptionClass(event.consumption)">{{ event.consumption }}</span></td>
            <td>
              <ol class="timeline compact">
                <li v-for="(item, idx) in event.history" :key="idx">
                  {{ formatTime(item.at) }} {{ item.action }}：{{ item.detail }}
                </li>
              </ol>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============================ 本地接收端 ============================ -->
    <div v-if="activeTab === 'receiver'" class="receiver-panel">
      <div class="panel-grid">
        <article class="panel-card">
          <h3>接收行为模式</h3>
          <p class="cell-sub">切换后下一次投递立即生效，用于制造拒收、超时与迟到回执场景。</p>
          <div class="mode-grid">
            <button
              v-for="mode in receiverModes"
              :key="mode.key"
              class="btn"
              :class="{ primary: receiverStatus.mode === mode.key }"
              type="button"
              @click="setMode(mode.key)"
            >
              {{ mode.label }}
            </button>
          </div>
        </article>

        <article class="panel-card">
          <h3>在线状态</h3>
          <p class="cell-sub">离线时投递连不上对端，按连接超时口径进入等待确认。</p>
          <button class="btn" :class="{ primary: receiverOnline }" type="button" @click="toggleOnline">
            {{ receiverOnline ? '置为离线（模拟故障）' : '恢复在线' }}
          </button>
        </article>

        <article class="panel-card">
          <h3>渠道登记</h3>
          <p class="cell-sub">未登记渠道的投递会被阻断，登记后即可续发。</p>
          <div class="inline-form">
            <input v-model="newChannel" placeholder="输入渠道名称，如 旧版短信网关" />
            <button class="btn primary" type="button" @click="registerChannel">登记渠道</button>
          </div>
          <div class="channel-list">
            <span v-for="channel in receiverStatus.channels" :key="channel" class="tag">{{ channel }}</span>
          </div>
        </article>

        <article class="panel-card">
          <h3>挂起投递与迟到回执</h3>
          <p class="cell-sub">挂起模式下接收端先收下、不回回执；可在这里补发，演示晚到回执与人工确认冲突。</p>
          <table v-if="held.length" class="data-table inner">
            <thead><tr><th>投递编号</th><th>渠道</th><th>挂起时间</th><th>补发回执</th></tr></thead>
            <tbody>
              <tr v-for="item in held" :key="item.delivery_id">
                <td>{{ item.delivery_id }}</td>
                <td>{{ item.channel }}</td>
                <td>{{ formatTime(item.received_at) }}</td>
                <td class="row-actions">
                  <button class="link" type="button" @click="releaseReceipt(item, 'accepted')">补发送达</button>
                  <button class="link" type="button" @click="releaseReceipt(item, 'rejected')">补发拒收</button>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty-state">当前没有挂起投递</div>
        </article>
      </div>

      <article class="panel-card">
        <h3>接收端收发日志（对端视角）</h3>
        <table class="data-table inner">
          <thead><tr><th>时间</th><th>投递编号</th><th>渠道</th><th>模式</th><th>事件</th><th>结论</th></tr></thead>
          <tbody>
            <tr v-for="(item, idx) in traffic" :key="idx">
              <td>{{ formatTime(item.at) }}</td>
              <td>{{ item.delivery_id }}</td>
              <td>{{ item.channel }}</td>
              <td>{{ item.mode }}</td>
              <td>{{ item.event }}</td>
              <td>{{ item.result || '—' }}</td>
            </tr>
            <tr v-if="!traffic.length"><td colspan="6" class="empty-state">暂无收发记录</td></tr>
          </tbody>
        </table>
      </article>
    </div>

    <!-- ============================ 回执台账 ============================ -->
    <div v-if="activeTab === 'receipts'">
      <h3>已关联回执</h3>
      <table class="data-table">
        <thead>
          <tr><th>投递编号</th><th>结论</th><th>签名校验</th><th>来源</th><th>签发时间</th><th>到达时间</th><th>备注</th></tr>
        </thead>
        <tbody>
          <tr v-for="receipt in receipts" :key="receipt.id">
            <td>{{ receipt.delivery_no }}</td>
            <td><span class="badge" :class="receipt.result === 'accepted' ? 'badge-delivered' : 'badge-rejected'">
              {{ receipt.result === 'accepted' ? '确认接收' : '拒收' }}
            </span></td>
            <td>
              <span class="tag" :class="receipt.valid_signature ? 'tag-ok' : 'tag-warn'">
                {{ receipt.valid_signature ? '签名有效' : '签名无效' }}
              </span>
            </td>
            <td>{{ receipt.source }}</td>
            <td>{{ formatTime(receipt.issued_at) }}</td>
            <td>{{ formatTime(receipt.received_at) }}</td>
            <td>{{ receipt.note || '—' }}</td>
          </tr>
          <tr v-if="!receipts.length"><td colspan="7" class="empty-state">暂无回执</td></tr>
        </tbody>
      </table>

      <h3 class="section-gap">未关联回执（疑似历史无投递标识记录）</h3>
      <p class="page-desc">关联不到投递项的回执不会被静默丢弃，单独落账供人工核对。</p>
      <table class="data-table">
        <thead><tr><th>回执标识</th><th>结论</th><th>来源</th><th>到达时间</th><th>隔离原因</th></tr></thead>
        <tbody>
          <tr v-for="receipt in unmatchedReceipts" :key="receipt.id">
            <td>{{ receipt.delivery_no }}</td>
            <td>{{ receipt.result === 'accepted' ? '确认接收' : '拒收' }}</td>
            <td>{{ receipt.source }}</td>
            <td>{{ formatTime(receipt.received_at) }}</td>
            <td class="block-reason">{{ receipt.reason }}</td>
          </tr>
          <tr v-if="!unmatchedReceipts.length"><td colspan="5" class="empty-state">没有未关联回执</td></tr>
        </tbody>
      </table>
    </div>

    <!-- ============================ 通知规则 ============================ -->
    <div v-if="activeTab === 'rules'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in ruleColumns" :key="column">{{ column }}</th>
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rules" :key="String(row.id)">
            <td v-for="column in ruleColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td><span class="badge" :class="row.status === '已启用' ? 'badge-delivered' : 'badge-not-sent'">{{ row.status }}</span></td>
            <td class="row-actions">
              <button v-for="action in ruleActions" :key="action" class="link" type="button" @click="runRuleAction(action, row)">
                {{ action }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ============================ 投递明细抽屉 ============================ -->
    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <aside class="drawer">
        <header class="drawer-head">
          <h3>{{ detail.delivery_no }} 投递明细</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </header>
        <div class="drawer-body">
          <dl class="detail-grid">
            <div><dt>投递状态</dt><dd><span class="badge" :class="statusClass(detail.status)">{{ detail.status_label }}</span></dd></div>
            <div><dt>尝试次数</dt><dd>{{ detail.attempt }}</dd></div>
            <div><dt>证据级别</dt><dd>{{ evidenceLabel(detail.evidence_level) }}</dd></div>
            <div><dt>关联事件</dt><dd>{{ detail.event_no }}（事件ID {{ detail.event_id }}）</dd></div>
            <div><dt>通知规则</dt><dd>{{ detail.rule_no || '历史记录无规则' }}</dd></div>
            <div><dt>渠道 / 接收方</dt><dd>{{ detail.channel }} / {{ detail.recipient }}</dd></div>
            <div class="full"><dt>阻断原因</dt><dd>{{ detail.block_reason_label || '无' }}</dd></div>
            <div class="full"><dt>事件摘要</dt><dd>{{ detail['事件摘要'] }}</dd></div>
          </dl>

          <h4>关联回执（乱序、重复只更新同一投递项）</h4>
          <table v-if="detailReceipts.length" class="data-table inner">
            <thead><tr><th>结论</th><th>签名</th><th>来源</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="receipt in detailReceipts" :key="receipt.id">
                <td>{{ receipt.result === 'accepted' ? '确认接收' : '拒收' }}</td>
                <td><span class="tag" :class="receipt.valid_signature ? 'tag-ok' : 'tag-warn'">
                  {{ receipt.valid_signature ? '有效' : '无效' }}
                </span></td>
                <td>{{ receipt.source }}</td>
                <td>{{ formatTime(receipt.received_at) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty-state">暂无回执，等待确认中的投递请耐心等待或人工核对</div>

          <h4>全流程留痕</h4>
          <ol class="timeline">
            <li v-for="(item, idx) in detail.history" :key="idx">
              <strong>{{ formatTime(item.at) }} {{ item.action }}</strong>
              <p>{{ item.detail }}<span v-if="item.operator"> · 操作人：{{ item.operator }}</span></p>
            </li>
          </ol>
        </div>
      </aside>
    </div>

    <!-- ============================ 重发确认弹窗 ============================ -->
    <div v-if="retryTarget" class="drawer-mask" @click.self="retryTarget = null">
      <aside class="modal">
        <h3>核对后重发：{{ retryTarget.delivery_no }}</h3>
        <p class="page-desc">
          该投递当前为「{{ retryTarget.status_label }}」。重发将<strong>复用同一条投递项</strong>并递增尝试次数，
          不会生成第二次相同投递。请确认已与对端核对、确实未收到。
        </p>
        <label class="filter-check">
          <input v-model="retryAck" type="checkbox" />
          <span>我已核对对端实际接收情况，确认需要重发</span>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="retryTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="!retryAck" @click="confirmRetry">确认重发</button>
        </div>
      </aside>
    </div>

    <!-- ============================ 登记事件弹窗 ============================ -->
    <div v-if="eventDialog" class="drawer-mask" @click.self="eventDialog = false">
      <aside class="modal">
        <h3>登记异常事件并建立投递</h3>
        <div class="form-stack">
          <label><span>事件摘要</span><input v-model="eventForm.summary" placeholder="如：核心接口错误率超阈值" /></label>
          <label><span>监控指标</span><input v-model="eventForm.metric" placeholder="如：HTTP 5xx 比例" /></label>
          <label><span>告警级别</span>
            <select v-model="eventForm.level">
              <option>P1</option><option>P2</option><option>P3</option>
            </select>
          </label>
          <label><span>通知渠道</span>
            <select v-model="eventForm.channel">
              <option v-for="channel in receiverStatus.channels" :key="channel" :value="channel">{{ channel }}</option>
            </select>
          </label>
          <label><span>通知规则</span>
            <select v-model="eventForm.rule_id">
              <option :value="null">不关联规则</option>
              <option v-for="rule in rules" :key="rule.id" :value="rule.id">
                {{ rule['规则编号'] }}（{{ rule.status }}）
              </option>
            </select>
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="eventDialog = false">取消</button>
          <button class="btn primary" type="button" @click="submitEvent">建立投递台账</button>
        </div>
      </aside>
    </div>

    <footer class="page-foot">
      <span>投递 {{ deliveries.length }} 条 · 事件 {{ events.length }} 条 · 回执 {{ receipts.length }} 张</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/notify'

const tabs = [
  { key: 'deliveries', label: '投递台账' },
  { key: 'events', label: '检测事件消费' },
  { key: 'receiver', label: '本地接收端' },
  { key: 'receipts', label: '回执台账' },
  { key: 'rules', label: '通知规则' },
]
const activeTab = ref('deliveries')

const statusLabels: Record<string, string> = {
  not_sent: '未发送',
  pending_ack: '等待确认',
  rejected: '对端拒收',
  delivered: '已确认送达',
}
const receiverModes = [
  { key: 'accept', label: '正常接收' },
  { key: 'reject', label: '对端拒收' },
  { key: 'timeout', label: '连接超时' },
  { key: 'hold', label: '挂起补发' },
]
const ruleColumns = ['规则编号', '触发条件', '通知渠道', '接收列表', '消息模板', '合并窗口秒', '重复抑制', '优先级']
const ruleActions = ['提交校验', '启用通知', '停用通知']

const deliveries = ref<Row[]>([])
const events = ref<Row[]>([])
const receipts = ref<Row[]>([])
const unmatchedReceipts = ref<Row[]>([])
const rules = ref<Row[]>([])
const receiverStatus = ref<Row>({ channels: [] })
const held = ref<Row[]>([])
const traffic = ref<Row[]>([])

const statusFilter = ref('')
const keyword = ref('')
const legacyOnly = ref(false)
const errorMessage = ref('')
const infoMessage = ref('')

const detail = ref<Row | null>(null)
const detailReceipts = ref<Row[]>([])
const retryTarget = ref<Row | null>(null)
const retryAck = ref(false)
const eventDialog = ref(false)
const eventForm = ref<Row>({ summary: '', metric: '', level: 'P2', channel: '本地演示通道', rule_id: 3 })
const newChannel = ref('')

const receiverOnline = computed(() => Boolean(receiverStatus.value.online))

const statCards = computed(() => {
  const counts: Record<string, number> = {}
  for (const row of deliveries.value) {
    counts[row.status_label] = (counts[row.status_label] || 0) + 1
  }
  return [
    { label: '未发送', value: counts['未发送'] || 0, cls: 'badge-not-sent' },
    { label: '等待确认', value: counts['等待确认'] || 0, cls: 'badge-pending' },
    { label: '对端拒收', value: counts['对端拒收'] || 0, cls: 'badge-rejected' },
    { label: '已确认送达', value: counts['已确认送达'] || 0, cls: 'badge-delivered' },
  ]
})

function statusClass(status: string): string {
  return {
    not_sent: 'badge-not-sent',
    pending_ack: 'badge-pending',
    rejected: 'badge-rejected',
    delivered: 'badge-delivered',
  }[status] || 'badge-not-sent'
}

function consumptionClass(consumption: string): string {
  if (consumption === '已处理') return 'badge-delivered'
  if (consumption === '等待投递确认') return 'badge-pending'
  if (consumption === '投递阻断') return 'badge-rejected'
  return 'badge-not-sent'
}

function evidenceLabel(level: string | null): string {
  if (level === 'signed_receipt') return '签名回执（可验证）'
  if (level === 'manual_confirmation') return '人工确认（较弱）'
  return '暂无证据'
}

function formatTime(epoch: number): string {
  if (!epoch) return '—'
  return new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false })
}

function flash(message: string, ok: boolean) {
  if (ok) {
    infoMessage.value = message
    errorMessage.value = ''
  } else {
    errorMessage.value = message
    infoMessage.value = ''
  }
  window.setTimeout(() => {
    errorMessage.value = ''
    infoMessage.value = ''
  }, 5000)
}

async function post(path: string, body: Record<string, any> = {}): Promise<Row> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return response.json()
}

async function reloadDeliveries() {
  const params = new URLSearchParams()
  if (statusFilter.value) params.set('status', statusFilter.value)
  if (keyword.value) params.set('keyword', keyword.value)
  if (legacyOnly.value) params.set('legacy_only', 'true')
  const response = await request(`${ENDPOINT}/deliveries?${params.toString()}`)
  const payload = await response.json()
  deliveries.value = payload.items ?? []
}

async function reloadEvents() {
  const response = await request(`${ENDPOINT}/events`)
  events.value = (await response.json()).items ?? []
}

async function reloadReceipts() {
  const response = await request(`${ENDPOINT}/receipts`)
  const payload = await response.json()
  receipts.value = payload.items ?? []
  unmatchedReceipts.value = payload.unmatched ?? []
}

async function reloadRules() {
  const response = await request(`${ENDPOINT}?size=200`)
  rules.value = (await response.json()).items ?? []
}

async function reloadReceiver() {
  const [statusResp, heldResp, trafficResp] = await Promise.all([
    request(`${ENDPOINT}/receiver/status`),
    request(`${ENDPOINT}/receiver/held`),
    request(`${ENDPOINT}/receiver/traffic`),
  ])
  receiverStatus.value = await statusResp.json()
  held.value = (await heldResp.json()).items ?? []
  traffic.value = (await trafficResp.json()).items ?? []
}

function switchTab(key: string) {
  activeTab.value = key
  if (key === 'events') void reloadEvents()
  if (key === 'receiver') void reloadReceiver()
  if (key === 'receipts') void reloadReceipts()
  if (key === 'rules') void reloadRules()
}

async function handleAction(action: string, row: Row) {
  try {
    if (action === '发起投递') {
      const result = await post(`${ENDPOINT}/deliveries/${row.id}/dispatch`, {})
      flash(result.message, result.ok)
    } else if (action === '核对后重发') {
      retryTarget.value = row
      retryAck.value = false
      return
    } else if (action === '人工确认送达' || action === '人工判定拒收') {
      const result = await post(`${ENDPOINT}/deliveries/${row.id}/manual-resolve`, {
        values: { outcome: action },
      })
      flash(result.message, result.ok)
    } else if (action === '核对历史记录') {
      const result = await post(`${ENDPOINT}/deliveries/${row.id}/verify-legacy`, {})
      flash(result.message, result.ok)
    }
    await reloadDeliveries()
    await reloadEvents()
  } catch (error) {
    flash(error instanceof Error ? error.message : '操作失败', false)
  }
}

async function confirmRetry() {
  if (!retryTarget.value) return
  const result = await post(`${ENDPOINT}/deliveries/${retryTarget.value.id}/retry`, {
    values: { acknowledge: retryAck.value },
  })
  retryTarget.value = null
  flash(result.message, result.ok)
  await reloadDeliveries()
  await reloadEvents()
}

async function openDetail(row: Row) {
  const response = await request(`${ENDPOINT}/deliveries/${row.id}`)
  const payload = await response.json()
  detail.value = payload.delivery
  detailReceipts.value = payload.receipts ?? []
}

async function resumeAll() {
  const result = await post(`${ENDPOINT}/resume`, {})
  flash(result.message, result.ok)
  await reloadDeliveries()
  await reloadEvents()
}

async function runRuleAction(action: string, row: Row) {
  const result = await post(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
  flash(result.message, result.ok)
  await reloadRules()
  await reloadDeliveries()
}

async function setMode(mode: string) {
  const result = await post(`${ENDPOINT}/receiver/mode`, { values: { mode } })
  flash(result.message, result.ok)
  await reloadReceiver()
}

async function toggleOnline() {
  const result = await post(`${ENDPOINT}/receiver/online`, { values: { online: !receiverOnline.value } })
  flash(result.message, result.ok)
  await reloadReceiver()
}

async function registerChannel() {
  if (!newChannel.value.trim()) return
  const result = await post(`${ENDPOINT}/receiver/channels`, { values: { channel: newChannel.value.trim() } })
  newChannel.value = ''
  flash(result.message, result.ok)
  await reloadReceiver()
}

async function releaseReceipt(item: Row, resultType: string) {
  const result = await post(`${ENDPOINT}/receiver/release-receipt`, {
    values: { delivery_no: item.delivery_id, result: resultType },
  })
  flash(result.message, result.ok)
  await Promise.all([reloadReceiver(), reloadDeliveries(), reloadReceipts(), reloadEvents()])
}

function openEventDialog() {
  eventDialog.value = true
}

async function submitEvent() {
  if (!eventForm.value.summary?.trim()) {
    flash('事件摘要不能为空', false)
    return
  }
  const eventResult = await post(`${ENDPOINT}/events`, { values: eventForm.value })
  if (!eventResult.ok) {
    flash(eventResult.message, false)
    return
  }
  const deliveryResult = await post(
    `${ENDPOINT}/events/${eventResult.entry.id}/deliveries`,
    { values: { channel: eventForm.value.channel, rule_id: eventForm.value.rule_id } },
  )
  eventDialog.value = false
  flash(deliveryResult.message, deliveryResult.ok)
  await Promise.all([reloadDeliveries(), reloadEvents()])
}

onMounted(() => {
  void Promise.all([reloadDeliveries(), reloadEvents(), reloadReceiver(), reloadRules(), reloadReceipts()])
})
</script>
