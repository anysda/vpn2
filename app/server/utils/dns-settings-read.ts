import { agh } from './agh-client'
import { settingsView, type DnsInfo, type LogConfig, type SafeSearch, type SettingsView, type StatsConfig } from './dns-settings'

export interface SettingsState { info: DnsInfo, sb: boolean, parental: boolean, ss: SafeSearch, log: LogConfig, stats: StatsConfig }

/** Всё, из чего собирается раздел «Настройки», одним заходом. */
export async function settingsState(): Promise<SettingsState> {
  const [info, sb, parental, ss, log, stats] = await Promise.all([
    agh<DnsInfo>('dns_info'),
    agh<{ enabled: boolean }>('safebrowsing/status'),
    agh<{ enabled: boolean }>('parental/status'),
    agh<SafeSearch>('safesearch/status'),
    agh<LogConfig>('querylog/config'),
    agh<StatsConfig>('stats/config'),
  ])
  return { info, sb: sb.enabled, parental: parental.enabled, ss, log, stats }
}

export const viewOf = (s: SettingsState): SettingsView => settingsView(s.info, s.sb, s.parental, s.ss, s.log, s.stats)
