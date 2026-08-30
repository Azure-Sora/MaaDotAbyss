# 14 · 连打任务的程序接管（auto 动作 + battle 宏）

2026-08-30 真机采样 + 落地。背景：adventure_forces 纯 LLM 逐步打，9 场战斗 ≈45 步 ≫
420s 预算（19:01 run 16 步断在 2/9 场）。结论：**选关不需要 LLM**——桥的 ui_tree
把开放状态/挑战回数全给了；LLM 只负责导航、调 auto、验证、返航。

## 1 · 为什么程序接管可行

桥 ui_tree 每节点带 `active`(activeSelf)/`text`/`button{interactable,path}`/`screen`，
canvas 级过滤 `activeInHierarchy`。判"开放"用**祖先 active 链**（eff-active），
因为隐藏占位卡的 `ButtonChallenge` 仍报 `interactable=True`（Milesgard STAGE99/×123456
占位数据实测），按钮标志不可信。

## 2 · 采样状态机（场景名全部实测）

### 2.1 势力任务 forces（入口场景 `UnionRequest`）
```
List_Country_{Milesgard,Peldion,Eldorana,Coalition,Luxnova}
  Open        eff-active = 今日开放（未开放时 active=False 且 Lock/ListEmpty<現在依頼はありません>可见）
    Label_Ribbon/TextTitle <ペルディオンSTAGE49>   # 关卡进度，胜场 +1
    ButtonChallenge/RootUI/Parts/Label/Layout/Text # 两个 Text：'挑戦回数\n' 与 '<color=..>N</color>/3'（N=剩余）
点 ButtonChallenge → Popup_UnionRequestDetail(Clone)：Button_Confirm<出撃> / Button_Cancel / Button_SkipMode<周回>
    ※ 周回模式禁用（用户：拿不全奖励）
点出撃 → Popup_Confirm_Sortie(Clone)：Button_Confirm / Button_Cancel
确定 → 场景 ExplorationBattle（自动战斗 ~20-35s）
     → 场景 ExploreResult：Button_Next<次へ>（还有 Button_Ranking，别点）
点次へ → 回 UnionRequest；首通弹 Popup_ClearRewards(Clone)（FullScreenCloseButton 关闭）
```

### 2.2 迎击战 disaster（入口场景 `DisasterTop`）
```
3 boss：ContentsR/DisasterArea/Area{1,2,3}/Disaster/RootUI（RootUI 即按钮）
Sp boss：SpDisasterArea/Disaster/RootUI <大厄災出現中> —— 不打（prompt 指定只打三个小 boss）
已讨伐判据：卡内 Label 的 推奨戦力/Lv 文本消失 + 出现 Anim/None/Button_Disaster 子按钮
Button_Crawl<巡回>：不碰（行为未知）
点 boss → Popup_QuestDetail_Disaster(Clone)：Button_Confirm<出撃> / Button_Cancel（无周回）
出撃 → Popup_Confirm_NoteButton2(Clone)<「…に出撃します。」>：Button_Confirm<決定> / Button_Cancel
确定 → 场景 DisasterBattle（持久战 ~77s，拠点防壁+倒计时）
     → 场景 DisasterResult：Button_Next
点次へ → 直接回 DisasterTop（无 ClearRewards 弹窗）
迎撃数 3/3 = 3 个 boss 委托（冒险页入口卡上显示）
```

### 2.3 探索任务 expedition（入口场景 `IdleExploration`）
```
入口：Top/Right/Button_EncountQuest <N件発生!>（页内每层还有 Encount/Button<探索クエスト発生中!>，同物）
     ※ 别点 Anim/Basic/Button_Exploration<第N階層を探索>——那是挂机探索队派遣（Popup_IdleExplorationParty）
任务列表（弹层，场景不变）：CellView(Clone)/EncounterQuestList/
    Button_Challenge<開始>；回数文本分裂节点：'挑戦回数【' '3' '/' '3' '】'（按容器聚合拼串再正则）
点開始 → Popup_QuestDetail_Exploration(Clone)：Button_Confirm<出撃> / Button_Cancel（ButtonSet3）
出撃 → 直接 ExplorationBattle（无二段确认）
     → ExploreResult(Button_Next) → 回 IdleExploration（列表弹层自动关闭）
回数耗尽再点開始 → 预期弹消費アビスジェム恢复确认 → 一律キャンセル（消费红线）→ 视为完成
```

### 2.4 结算路上可能的弹窗（三处通用）
- 自動分解確認 → 点文本<分解する>；分解報酬 → Popup_Close/X
- Popup_ClearRewards → FullScreenCloseButton
- 消費/回復/購入句式 → Button_Cancel（消费红线，绝不決定）

## 3 · 架构：LLM 骨架 + auto 程序连打

```
run_task(LLM 循环)
  └─ action=auto {"routine":"forces_sweep|disaster_sweep|expedition_sweep"}
       └─ routines.py：入口场景校验 → 读卡/读boss/读任务 → 循环[点出撃链 → battle 宏 → 回列表验证]
            └─ macros.py：wait_scene / settle_to / 通用弹窗 / eff-active 树工具
异常/打完 → 结构化结果回填 history → LLM 继续（验证+返航+report done）
```

- LLM 步数 45 → 6-8（导航 2-3 + auto 1 + 验证返航 2-3）
- routine 全程 click_by_path（精确路径，零穿透，天然避开 Footer 的 Gacha/R18 禁区按钮）；
  skip_page 仅用于无按钮结算页（点 (0,0)，与深渊 _result_step 同款铁律）
- auto 执行期间 run_task 时间预算不推进（步边界才检查，auto 耗时从 t0 扣除）；routine
  自带总超时（forces 1200s / disaster 900s / expedition 1500s）与场数上限
- GUI：ACTION_ZH["auto"]="程序接管"；出发/收尾各发一张 StepCard，进度走 log 通道 +
  frame_cb 刷预览
- 失败语义：单场战斗后计数未递减记 `suspect`（延迟 2s 重读一次再定责），连续 2 次 →
  返回 partial（LLM 兜底决断）；场景漂移 → 立即返回，绝不盲试
- 真机验证（2026-08-30）：forces 6 场一步清完（31s~121s/场）；disaster 1 场完整循环
  81s；expedition 次数读数+done 路径通过；auto 被三个任务的首步正确触发

## 3.1 · 两个差点翻车的坑（2026-08-30 首跑实测）

1. **转场 loading 卡屏**：点击打断 Unity CommonLoad 转场 → NOW LOAD 气泡卡死、
   全屏无响应（两次实测，用户手动重启游戏）。可靠信号：`Transition` canvas 的
   eff-active 节点数平时=2（Transition+TransitionService），转场时整组激活（~20，
   持续 1-2s）。**场景名在转场中段就切换、画面 diff 也可能很小**——wait_settled
   有盲区，不能靠它们。对策：`wait_transition_done()`（先睡 0.5s 给转场启动时间
   再轮询——点击到转场层激活有延迟窗口）接入所有点击后路径；LLM click/skip 分支
   转场未结束即熔断 blocked。
2. **ui_tree max_nodes 截断**：canvas 遍历顺序不可控（FindObjectsOfType 无序），
   max_nodes=8000 时排后面的 UICanvas/Front 画布可能被整体截掉——disaster 结算页
   「次へ」找不到死循环 300s、expedition 入口找不到，同根因。对策：全树读取一律
   30000；弹窗类单独 `canvas="Front"` 小树读取。

## 3.2 · 游戏机制备注

- 势力任务各关卡每日独立 3 次；迎击战 = 3 个 boss 委托各 1 次（迎撃数 3/3）；
- **探索任务全部任务共享 3 次总次数**（各卡挑戦回数同步显示同一数字），打完一个
  任务该任务消失、下一个顶上——盯"第一个可用条目"打到 0 即可；
- 周回(Button_SkipMode)禁用：拿不全奖励（用户明确要求）。

## 4 · 采样日实况（2026-08-30）

forces：Peldion STAGE49 推奨134k vs 我方86k 照样赢（33s），STAGE→50，计数 1/3→0/3。
disaster：Area1 倦怠之灾厄 97.6k vs 127k 赢（77s）。
expedition：第一个任务（後衛煉獄 掃討 深度10，推奨68.9k）赢（21s），挑戦回数 3/3→2/3。
