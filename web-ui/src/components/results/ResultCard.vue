<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultItem } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye, RotateCcw, PackageX, Tag } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
  (e: 'set-listing-status', item: ResultItem, status: 'sold' | 'delisted' | 'unknown'): void
}>()
const { t } = useI18n()

const info = props.item.商品信息
const seller = props.item.卖家信息
const ai = props.item.ai_analysis
const priceInsight = props.item.price_insight

const isRecommended = ai?.is_recommended === true
const recommendationStatus = computed(() => {
  if (ai?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})

const imageUrl = info.商品图片列表?.[0] || info.商品主图链接 || ''
const crawlTime = props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')
const matchScore = ai?.value_score ?? 0
const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

// 商品在闲鱼上的在售状态，与 _status（可见性）互不影响：已售/已下架仍然显示。
const listingStatus = computed(() => props.item._listing_status || 'unknown')
const statusMeta = computed(() => {
  switch (listingStatus.value) {
    case 'active':
      return { label: t('results.status.active'), badge: 'bg-emerald-500/90' }
    case 'sold':
      return { label: t('results.status.sold'), badge: 'bg-rose-500/90' }
    case 'delisted':
      return { label: t('results.status.delisted'), badge: 'bg-slate-600/90' }
    default:
      return { label: t('results.status.unknown'), badge: 'bg-amber-500/90' }
  }
})
const isTerminated = computed(() => listingStatus.value === 'sold' || listingStatus.value === 'delisted')
const isSold = computed(() => listingStatus.value === 'sold')
const isDelisted = computed(() => listingStatus.value === 'delisted')
// 人工标记的记录：平台探测不会覆盖它，界面上要能区分出来
const isManualStatus = computed(() => props.item._status_source === 'manual')
const manualMarkedTitle = computed(() => {
  const checkedAt = props.item._status_checked_at
  if (!checkedAt) return t('results.card.manualTag')
  return t('results.status.manualMarkedAt', { time: formatDateTime(checkedAt) })
})
// 「标记已售」「标记已下架」两个按钮：各自表示切到另一种人工状态，
// 已经是该状态时按钮变成「撤销」（unknown），一次点击就能改回未知。
const soldAction = computed<'sold' | 'unknown'>(() => (isSold.value ? 'unknown' : 'sold'))
const delistedAction = computed<'delisted' | 'unknown'>(() => (isDelisted.value ? 'unknown' : 'delisted'))
const soldActionLabel = computed(() =>
  isSold.value ? t('results.card.unmarkSold') : t('results.card.markSold')
)
const delistedActionLabel = computed(() =>
  isDelisted.value ? t('results.card.unmarkDelisted') : t('results.card.markDelisted')
)
const isSoldPriceEstimated = computed(
  () => listingStatus.value === 'sold' && props.item._sold_price_source === 'listing_fallback'
)
const soldPriceText = computed(() => {
  if (listingStatus.value !== 'sold' || props.item._sold_price === null || props.item._sold_price === undefined) {
    return ''
  }
  if (isSoldPriceEstimated.value) {
    return t('results.card.soldPriceEstimated', { price: props.item._sold_price })
  }
  return props.item._sold_price_display || `¥${props.item._sold_price}`
})
// 成交价与「市场均价 / 历史低位」并排展示时，格子里只放数值，标签由上方标题承担
const soldPriceValue = computed(() => {
  if (!soldPriceText.value) return ''
  if (isSoldPriceEstimated.value) {
    return t('results.card.soldPriceEstimatedValue', { price: props.item._sold_price })
  }
  return props.item._sold_price_display || `¥${props.item._sold_price}`
})
const hasPriceStats = computed(() => Boolean(priceInsight?.observation_count))
const priceStatsGridClass = computed(() => {
  if (soldPriceText.value && hasPriceStats.value) return 'grid-cols-3'
  if (hasPriceStats.value) return 'grid-cols-2'
  return 'grid-cols-1'
})

const expanded = ref(false)
</script>

<template>
  <Card class="group flex flex-col h-full border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden, 'saturate-50': isTerminated && !isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[4/3] overflow-hidden">
      <div class="absolute inset-0 bg-slate-200 animate-pulse" v-if="!imageUrl"></div>
      <img
        v-else
        :src="imageUrl"
        :alt="info.商品标题"
        class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
        loading="lazy"
      />
      <!-- Hidden overlay -->
      <div v-if="isHidden" class="absolute inset-0 bg-black/30 flex items-center justify-center">
        <span class="text-white/80 text-xs font-semibold uppercase tracking-wider">{{ hiddenLabel }}</span>
      </div>
      <!-- Overlays -->
      <div class="absolute top-3 left-3 flex gap-2">
        <Badge v-if="isRecommended && !isHidden" variant="default" class="bg-emerald-500/90 backdrop-blur-md border-none shadow-sm">
          {{ t('results.card.curated') }}
        </Badge>
        <Badge v-if="isRuleHidden" variant="secondary" class="bg-slate-900/75 text-white border-none backdrop-blur-md shadow-sm">
          {{ t('results.card.blacklisted') }}
        </Badge>
        <Badge
          v-if="listingStatus !== 'unknown'"
          variant="secondary"
          class="backdrop-blur-md border-none shadow-sm text-white"
          :class="statusMeta.badge"
        >
          {{ statusMeta.label }}
        </Badge>
      </div>
      <!-- 人工标记标识放在图片左下角，避免和右上角图标按钮抢位置 -->
      <div v-if="isManualStatus && listingStatus !== 'unknown'" class="absolute bottom-3 left-3">
        <Badge
          variant="secondary"
          class="backdrop-blur-md border-none shadow-sm text-white bg-indigo-500/90"
          :title="manualMarkedTitle"
          :aria-label="manualMarkedTitle"
        >
          {{ t('results.status.manual') }}
        </Badge>
      </div>
      <div class="absolute top-3 right-3 flex gap-1.5">
        <button
          v-if="canToggleBlock"
          type="button"
          @click="emit('toggle-block', props.item)"
          :aria-label="isHidden ? t('results.card.unblock') : t('results.card.block')"
          class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-white/50"
        >
          <EyeOff v-if="!isHidden" class="w-4 h-4" />
          <Eye v-else class="w-4 h-4" />
        </button>
         <a
           :href="info.商品链接"
           target="_blank"
           rel="noopener noreferrer"
           :aria-label="t('results.card.detail')"
           class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
         >
            <ExternalLink class="w-4 h-4" />
         </a>
      </div>
    </div>

    <CardHeader class="p-4 pb-2">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="text-base font-semibold text-slate-800 line-clamp-2 leading-snug flex-grow h-10">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-2">
        <span class="text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="info['商品原价']" class="text-xs text-slate-400 line-through mb-1">{{ info['商品原价'] }}</span>
      </div>
    </CardHeader>

    <CardContent class="p-4 pt-2 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-3 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
          </div>
          <div class="flex items-center gap-1">
             <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">AI Match</span>
             <span class="text-sm font-black" :class="recommendationStatus.text">{{ matchScore }}%</span>
          </div>
        </div>
        
        <div class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-3">
          <div 
            class="h-full transition-all duration-1000 ease-out rounded-full" 
            :class="recommendationStatus.color"
            :style="{ width: `${matchScore}%` }"
          ></div>
        </div>

        <p class="text-xs leading-relaxed text-slate-600" :class="{ 'line-clamp-2': !expanded }">
           {{ ai?.reason || t('results.card.analyzing') }}
        </p>
        
        <button
          type="button"
          v-if="ai?.reason && ai.reason.length > 50"
          @click="expanded = !expanded" 
          class="mt-1 text-[10px] font-bold uppercase text-primary/70 hover:text-primary transition-colors flex items-center gap-1"
        >
          {{ expanded ? t('results.card.collapse') : t('results.card.expand') }}
          <Info class="w-3 h-3" />
        </button>
      </div>

      <!-- 价格指标：成交价 / 市场均价 / 历史低位 同一排 -->
      <div v-if="soldPriceText || hasPriceStats" class="mt-4 grid gap-3" :class="priceStatsGridClass">
        <!-- 成交价 -->
        <div v-if="soldPriceText" class="p-2.5 rounded-xl border border-rose-100 bg-rose-50/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-rose-400 mb-1">
            <Tag class="w-3 h-3" /> {{ t('results.card.soldPrice') }}
          </div>
          <div class="text-sm font-bold text-rose-600">{{ soldPriceValue }}</div>
          <p v-if="isSoldPriceEstimated" class="mt-0.5 text-[9px] text-rose-400">
            {{ t('results.card.soldPriceEstimatedHint') }}
          </p>
        </div>
        <!-- 市场均价 -->
        <div v-if="hasPriceStats" class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingUp class="w-3 h-3" /> {{ t('results.card.marketAvg') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight?.market_avg_price ? `¥${priceInsight.market_avg_price}` : '—' }}
          </div>
        </div>
        <!-- 历史低位 -->
        <div v-if="hasPriceStats" class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingDown class="w-3 h-3" /> {{ t('results.card.historicalLow') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight?.min_price ? `¥${priceInsight.min_price}` : '—' }}
          </div>
        </div>
      </div>
    </CardContent>

    <CardFooter class="px-4 py-3 bg-slate-50/30 border-t border-slate-100/60 flex flex-wrap items-center justify-between gap-2 text-[10px]">
      <div class="flex items-center gap-3 text-slate-400 min-w-0 overflow-hidden">
        <div class="flex items-center gap-1 shrink-0">
          <User class="w-3 h-3" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex items-center gap-1 shrink-0">
          <Clock class="w-3 h-3" />
          <span>{{ crawlTime }}</span>
        </div>
      </div>
      <div class="flex items-center gap-1.5 shrink-0">
        <button
          type="button"
          @click="emit('set-listing-status', props.item, soldAction)"
          :aria-label="soldActionLabel"
          class="inline-flex items-center gap-1 rounded-full border px-2 py-1 font-semibold transition-colors"
          :class="isSold
            ? 'border-slate-300 bg-white text-slate-500 hover:border-slate-400 hover:text-slate-700'
            : 'border-rose-200 bg-rose-50 text-rose-600 hover:border-rose-300 hover:bg-rose-100'"
        >
          <RotateCcw v-if="isSold" class="w-3 h-3" />
          <CheckCircle2 v-else class="w-3 h-3" />
          <span>{{ soldActionLabel }}</span>
        </button>
        <button
          type="button"
          @click="emit('set-listing-status', props.item, delistedAction)"
          :aria-label="delistedActionLabel"
          class="inline-flex items-center gap-1 rounded-full border px-2 py-1 font-semibold transition-colors"
          :class="isDelisted
            ? 'border-slate-300 bg-white text-slate-500 hover:border-slate-400 hover:text-slate-700'
            : 'border-slate-300 bg-slate-100 text-slate-600 hover:border-slate-400 hover:bg-slate-200'"
        >
          <RotateCcw v-if="isDelisted" class="w-3 h-3" />
          <PackageX v-else class="w-3 h-3" />
          <span>{{ delistedActionLabel }}</span>
        </button>
        <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
          {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
        </a>
      </div>
    </CardFooter>
  </Card>
</template>
