<script setup lang="ts">
useHead({ title: 'Настройки — anysda-vpn2' })
definePageMeta({ title: 'Настройки', sub: 'Бот, профиль и вход в панель' })
</script>

<template>
  <main class="tab on stg">
    <div class="head">
      <div>
        <h1>Настройки</h1>
        <p>Бот, профиль и вход в панель</p>
      </div>
    </div>
    <section class="sgrid">
      <SettingsProfile />
      <BotsSection />
      <SettingsSso />
    </section>
  </main>
</template>

<style>
/* Настройки: классы макета #t-settings, под корнем .stg */
.stg .sh h3{color:var(--t1);font-size:15px}
.stg .ha{display:flex;gap:8px;align-items:center}
.stg .sbig{margin:18px 0 14px}
.stg .sbig .big{font-size:34px}
.stg .ys{display:grid;grid-template-columns:repeat(3,1fr);margin:0 0 6px}
.stg .ys div{display:flex;flex-direction:column;gap:4px;padding-left:14px;border-left:1px solid rgba(var(--fg),.07)}
.stg .ys div:first-child{padding-left:0;border-left:0}
.stg .ys span{font-size:11.5px;color:var(--t3)}
.stg .ys b{font-weight:400;font-size:17px}
.stg .fld .in{flex:1;height:36px}
.stg .fld .hl{flex:none;max-width:210px;font-size:11.5px;color:var(--t3);line-height:1.35}
.stg .sub2{color:var(--t3);font-size:12.5px;margin:0}
.stg .fld code.sub2{color:var(--t3);font-size:12.5px}
.stg .al b{color:var(--t1);font-weight:500}
.stg .al.red b{color:var(--red-t)}
.stg .sc .al+.fld,.stg .sc .al+.pwf>.fld:first-child{margin-top:10px}
.stg .pwf{display:contents}
.stg fieldset.fg2{border:0;margin:0;padding:0;min-width:0}
.stg .ssso{grid-column:2;grid-row:1/3}
@media (hover:hover){.stg .xb:hover{color:var(--t1)}}
.stg #tfa{margin-top:22px}
.stg #tfa .sh{padding-top:16px;border-top:1px solid rgba(var(--fg),.08)}
.stg #tfa .mt{margin-top:14px}
.stg #tfa form{display:contents}
.stg .qrrow{display:flex;gap:18px;align-items:flex-end}
.stg .qrrow .in{margin-bottom:4px}
.stg #tfa .qr{flex:none;display:block;max-width:100%;margin:14px 0 0;padding:12px;border-radius:18px;background:#fff;box-shadow:0 0 40px rgba(var(--glow),calc(.12*var(--gk)))}
.stg #tfa .qr svg{display:block;max-width:100%;height:auto}
.stg #tfa .fld.qrcode{margin-top:14px}
/* узкий ПК: в две колонки подписи и поля не вмещаются — карточки одна под другой */
@media (min-width:721px) and (max-width:1000px){
  .stg .sgrid{grid-template-columns:minmax(0,1fr)}
  .stg .ssso{grid-column:auto;grid-row:auto}
}
/* телефон: одна колонка, подпись над значением, ряды и кнопки как в мобильном макете */
@media (max-width:720px){
  .stg .sgrid{grid-template-columns:minmax(0,1fr)}
  .stg .ssso{grid-column:auto;grid-row:auto}
  .stg .sc{padding:20px 18px;min-height:0}
  .stg .sh{gap:8px}
  .stg .sc>.al{margin-top:4px}
  .stg .sc div.fld:not(:has(>button))>span:first-child{width:120px}
  /* поле: подпись сверху, под полем подсказка; кнопка «показать» у поля справа */
  .stg .sc label.fld,.stg .sc .al+.pwf>label.fld:first-child{flex-wrap:wrap;gap:7px 8px;padding:0;margin-top:14px;border-top:0}
  .stg .sc label.fld>span:first-child{width:auto;flex:1 0 100%;font-size:12px}
  .stg .sc label.fld .in{height:40px}
  .stg .sc label.fld .hl{flex:1 0 100%;max-width:none}
  /* ряд из одних кнопок — справа */
  .stg .sc div.fld:has(>span:empty),.stg .sc div.fld:has(>code:empty){justify-content:flex-end;gap:8px;padding:0;margin-top:14px;border-top:0}
  .stg .sc div.fld:has(>code:empty)>span,.stg .sc div.fld>span:empty,.stg .sc div.fld>code:empty{display:none}
  .stg #tfa div.fld:has(>code:empty){margin-top:12px}
  /* тумблеры и проверка: подпись и пояснение слева, кнопка справа */
  .stg .ssso div.fld:not(:has(>code:empty)){display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:12px;align-items:center;padding:12px 0;margin-top:14px;border-top:1px solid rgba(var(--fg),.05)}
  .stg .ssso div.fld:not(:has(>code:empty))+div.fld{margin-top:0}
  .stg .ssso div.fld:not(:has(>code:empty))>span:first-child{grid-area:1/1;width:auto;color:var(--t1);font-size:13px}
  .stg .ssso div.fld:not(:has(>code:empty))>code{grid-area:2/1;color:var(--t3);font-size:11.5px;line-height:1.45}
  .stg .ssso div.fld:not(:has(>code:empty))>button{grid-area:1/2/3/3}
  .stg .ssso div.fld:has(>code.sub2){margin-top:12px}
  .stg .ssso div.fld:has(>code.sub2)>span:first-child{color:var(--t3);font-size:12.5px}
  .stg .ssso div.fld:has(>code.sub2)>code{font-size:12px}
  .stg .sc div.fld:has(>code.sub2)+label.fld{margin-top:4px}
  .stg .ssso fieldset>div.fld:last-child{padding-top:12px;border-top:1px solid rgba(var(--fg),.05)}
  /* бот: длинный ник и подписи счётчиков не раздвигают карточку */
  .stg .sbig .big{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .stg .ys{grid-template-columns:repeat(3,minmax(0,1fr))}
  .stg .ys span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* настройка 2FA: QR над секретом */
  .stg .qrrow{flex-direction:column;align-items:stretch;gap:12px}
  .stg #tfa .qr{align-self:center}
  .stg .qrrow .in{margin-bottom:0;height:40px}
  .stg button{touch-action:manipulation}
  .stg .pill,.stg .tog,.stg .xb{-webkit-user-select:none;user-select:none}
  .stg.bs .fl2 .in{height:44px}
  .stg.bs .mf{margin-top:18px}
}
</style>
