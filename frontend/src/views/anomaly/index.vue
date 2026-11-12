<template>
  <section class="page" data-module="anomaly">
    <header class="page-head">
      <div>
        <h2>异常检测管理</h2>
        <p class="page-desc">维护异常检测规则的输入、执行约束与结果记录，为数据异常发现提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记检测规则</button>
        <button class="btn" type="button" @click="exportRows">导出异常检测清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无异常检测数据，可先登记检测规则</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-gap">检测事件的通知消费结论（与通知分发模块同步）</h3>
    <p class="page-desc">
      只要关联通知还处于「等待投递确认」，原检测事件就保持未处理，不会被提前关闭；
      拒收闭环或确认送达后才标记为已处理。
    </p>
    <table class="data-table">
      <thead>
        <tr><th>事件编号</th><th>来源规则</th><th>监控指标</th><th>级别</th><th>事件摘要</th><th>通知消费结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="event in notifyEvents" :key="event.id">
          <td>{{ event.event_no }}</td>
          <td>{{ event.source_rule || '—' }}</td>
          <td>{{ event.metric }}</td>
          <td>{{ event.level }}</td>
          <td>{{ event.summary }}</td>
          <td><span class="badge" :class="consumptionClass(event.consumption)">{{ event.consumption }}</span></td>
        </tr>
        <tr v-if="!notifyEvents.length">
          <td colspan="6" class="empty-state">暂无检测事件</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条异常检测记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/anomaly'
const columns = ["规则编号", "监控指标", "基线均值", "标准差倍数", "检测周期", "告警级别", "静默窗口分钟", "通知渠道"]
const actions = ["提交校验", "启用检测", "停用检测"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const stats = [{"label": "检测规则总数", "value": 0}, {"label": "已启用规则", "value": 0}, {"label": "待校验规则", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const notifyEvents = ref<Record<string, any>[]>([])

function consumptionClass(consumption: string): string {
  if (consumption === '已处理') return 'badge-delivered'
  if (consumption === '等待投递确认') return 'badge-pending'
  if (consumption === '投递阻断') return 'badge-rejected'
  return 'badge-not-sent'
}

async function reloadNotifyEvents() {
  try {
    const response = await request('/api/anomaly/events')
    if (!response.ok) return
    const payload = await response.json()
    notifyEvents.value = payload.items ?? []
  } catch {
    // 事件消费结论是辅助视图，读取失败不阻断规则管理
    notifyEvents.value = []
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '检测规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('异常检测动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常检测操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('检测规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常检测列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadNotifyEvents()
})
</script>
