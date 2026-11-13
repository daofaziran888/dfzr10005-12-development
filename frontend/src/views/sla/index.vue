<template>
  <section class="page" data-module="sla">
    <header class="page-head">
      <div>
        <h2>SLA告警管理管理</h2>
        <p class="page-desc">维护SLA告警的产出时间约束、延迟阈值与升级策略，为SLA保障提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记SLA规则</button>
        <button class="btn" type="button" @click="exportRows">导出SLA告警管理清单</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无SLA告警管理数据，可先登记SLA规则</td>
        </tr>
      </tbody>
    </table>

    <section class="risk-board">
      <h3>风险窗口推演</h3>
      <p class="risk-desc">
        按基线承诺时间、关键路径最近产出与延迟阈值推演预警起点与最晚处置时间；基线与SLA共用同一份时间快照，
        同一监控对象在去重键下只保留一条有效升级记录。
      </p>
      <div class="risk-controls">
        <label class="filter-item">
          <span>时间快照（留空取当前时间）</span>
          <input v-model="snapshotAt" placeholder="如 2026-11-13 08:20" />
        </label>
        <button class="btn primary" type="button" @click="deriveWindows">推演风险窗口</button>
        <button class="btn" type="button" @click="loadRisk">刷新处置队列</button>
        <span v-if="riskMessage" class="risk-message">{{ riskMessage }}</span>
      </div>

      <h4>处置队列（按风险优先级排序）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in queueColumns" :key="column">{{ column }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in queueRows" :key="String(item['窗口编号'])">
            <td v-for="column in queueColumns" :key="column">{{ item[column] ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="closeWindow(item)">关闭</button>
            </td>
          </tr>
          <tr v-if="!queueRows.length">
            <td :colspan="queueColumns.length + 1" class="empty-state">暂无活动风险窗口，可先推演</td>
          </tr>
        </tbody>
      </table>

      <h4>风险窗口（含历史与关闭结论）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in windowColumns" :key="column">{{ column }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="window in windowRows" :key="String(window['窗口编号'])">
            <td v-for="column in windowColumns" :key="column">{{ window[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-if="isActive(window)"
                class="link"
                type="button"
                @click="occupyEscalation(window)"
              >
                占用升级额度
              </button>
              <button
                v-if="isActive(window)"
                class="link"
                type="button"
                @click="closeWindow(window)"
              >
                关闭
              </button>
            </td>
          </tr>
          <tr v-if="!windowRows.length">
            <td :colspan="windowColumns.length + 1" class="empty-state">暂无风险窗口记录</td>
          </tr>
        </tbody>
      </table>

      <h4>异常 / 恢复事件上报</h4>
      <form class="filter-bar" @submit.prevent="submitEvent">
        <label class="filter-item">
          <span>规则编号</span>
          <input v-model="eventForm.规则编号" placeholder="如 SLA-0003" />
        </label>
        <label class="filter-item">
          <span>事件类型</span>
          <select v-model="eventForm.事件类型">
            <option value="异常">异常</option>
            <option value="恢复">恢复</option>
          </select>
        </label>
        <label class="filter-item">
          <span>事件时间</span>
          <input v-model="eventForm.事件时间" placeholder="如 2026-11-13 17:45" />
        </label>
        <label class="filter-item">
          <span>说明（恢复时作为关闭依据）</span>
          <input v-model="eventForm.说明" placeholder="可留空，自动生成" />
        </label>
        <button class="btn" type="submit">上报事件</button>
      </form>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条SLA告警管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RiskRow = Record<string, string | number | null | undefined>

const ENDPOINT = '/api/sla'
const RISK_ENDPOINT = '/api/sla/risk'
const columns = ["规则编号", "监控对象", "产出时间", "延迟阈值", "告警级别", "通知渠道", "升级策略", "规则状态"]
const actions = ["启用监控", "处理告警", "关闭规则"]
const statuses = ["草稿", "监控中", "已触发", "已关闭"]
const stats = [{"label": "SLA规则总数", "value": 0}, {"label": "监控中规则", "value": 0}, {"label": "已触发规则", "value": 0}]
const queueColumns = ["优先级分", "窗口编号", "监控对象", "风险等级", "状态", "预警起点", "最晚处置时间", "剩余分钟", "通知渠道", "去重键", "升级编号"]
const windowColumns = ["窗口编号", "规则编号", "基线编号", "监控对象", "承诺时间", "预警起点", "最晚处置时间", "状态", "口径版本", "关闭时间", "关闭依据"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const snapshotAt = ref('')
const riskMessage = ref('')
const queueRows = ref<RiskRow[]>([])
const windowRows = ref<RiskRow[]>([])
const eventForm = ref({ 规则编号: 'SLA-0003', 事件类型: '异常', 事件时间: '2026-11-13 17:45', 说明: '' })

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = 'SLA规则登记入口尚未接入审批流'
}

function isActive(window: RiskRow) {
  return ['预警中', '已超限', '已升级'].includes(String(window['状态']))
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('SLA告警管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA告警管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('SLA规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SLA告警管理列表读取失败'
  }
}

async function postRisk(path: string, values: Record<string, string | undefined>) {
  const response = await request(`${RISK_ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  riskMessage.value = payload.message ?? ''
  return payload
}

async function deriveWindows() {
  riskMessage.value = ''
  try {
    await postRisk('/derive', { 时间快照: snapshotAt.value || undefined })
    await loadRisk()
  } catch (error) {
    riskMessage.value = error instanceof Error ? error.message : '风险窗口推演失败'
  }
}

async function loadRisk() {
  riskMessage.value = ''
  try {
    const query = snapshotAt.value ? `?snapshot_at=${encodeURIComponent(snapshotAt.value)}` : ''
    const [queueResponse, windowsResponse] = await Promise.all([
      request(`${RISK_ENDPOINT}/queue${query}`),
      request(`${RISK_ENDPOINT}/windows`),
    ])
    if (!queueResponse.ok || !windowsResponse.ok) {
      throw new Error('风险窗口数据读取失败')
    }
    const queuePayload = await queueResponse.json()
    const windowsPayload = await windowsResponse.json()
    queueRows.value = queuePayload.items ?? []
    windowRows.value = windowsPayload.items ?? []
  } catch (error) {
    riskMessage.value = error instanceof Error ? error.message : '风险窗口数据读取失败'
  }
}

async function submitEvent() {
  riskMessage.value = ''
  try {
    await postRisk('/events', { ...eventForm.value })
    await loadRisk()
  } catch (error) {
    riskMessage.value = error instanceof Error ? error.message : '事件上报失败'
  }
}

async function occupyEscalation(riskWindow: RiskRow) {
  riskMessage.value = ''
  try {
    await postRisk('/escalations', { 窗口编号id: String(riskWindow.id ?? '') })
    await loadRisk()
  } catch (error) {
    riskMessage.value = error instanceof Error ? error.message : '升级额度占用失败'
  }
}

async function closeWindow(riskWindow: RiskRow) {
  const conclusion = window.prompt(`为 ${riskWindow['窗口编号']} 填写关闭依据（可追溯的关闭结论）`)
  if (!conclusion) {
    return
  }
  riskMessage.value = ''
  try {
    await postRisk(`/windows/${riskWindow.id}/close`, { 关闭依据: conclusion, 关闭时间: snapshotAt.value || undefined })
    await loadRisk()
  } catch (error) {
    riskMessage.value = error instanceof Error ? error.message : '窗口关闭失败'
  }
}

onMounted(() => {
  void reload()
  void loadRisk()
})
</script>

<style scoped>
.risk-board {
  margin-top: 20px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.risk-board h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.risk-board h4 {
  margin: 16px 0 8px;
  font-size: 13px;
}
.risk-desc {
  color: var(--muted);
  font-size: 12px;
  margin: 0 0 10px;
}
.risk-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 8px;
}
.risk-message {
  font-size: 12px;
  color: var(--muted);
}
.risk-board select {
  display: block;
}
</style>
