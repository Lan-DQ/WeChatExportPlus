# -*- coding: utf-8 -*-
"""按批次把文件自动投喂给 DeepSeek 网页版。

用户定的规则（照抄需求）
------------------------
1. 一次最多 50 个文件，超了就分批；
2. 每批发出去以后模型会自动开始思考，**在下一批之前要点右下角的「截断/停止」**，
   否则输入区是禁用状态，挂不上新附件；
3. 最后一批**不截断**（让它正常回答）；
4. 全自动，点一次发送就把所有批次发完。

本模块只做调度；「怎么找到上传框、怎么点停止」在 Electron 侧（dsview/），
通过 ds_bridge.host.DeepSeekHost 的 HTTP 接口调用。
"""
import os
import random
import time

DEFAULT_LIMIT = 30
# 每个附件"挂上"之后还要等多久才点发送。
# 用户先定 0.5/0.3 秒，实测太保守（30 个文档要等 15 秒），后来要求"下调一半"，
# 于是改成 0.25/0.15 秒。等太短的话官网会报"服务器繁忙/请删除异常文件再发送"，
# 所以这个值不建议再往下降。
DOC_SETTLE_MS = 250
IMG_SETTLE_MS = 150
IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.tif',
            '.tiff', '.avif', '.svg', '.ico'}


def settle_ms_for(paths):
    """按扩展名算这批文件需要的处理等待时间（毫秒）。"""
    total = 0
    for p in paths or []:
        ext = os.path.splitext(str(p))[1].lower()
        total += IMG_SETTLE_MS if ext in IMG_EXTS else DOC_SETTLE_MS
    return min(60000, total)


def fmt_dur(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f'{sec} 秒'
    return f'{sec // 60} 分 {sec % 60} 秒'
# 两个批次之间的空档（秒）：稍微随机化，别像机器一样精确 —— 降低被风控盯上的概率
DEFAULT_PACE = (2.5, 4.5)
# 发出去之后等模型"开始思考"的上限（秒）。等到了就说明发送成功
STREAM_START_TIMEOUT = 12.0
# 确认开始生成后，再停留多久才点停止（太早可能按钮还没出现，太晚就白烧 token）
SETTLE_BEFORE_STOP = 1.0
# 等"停止"生效/附件清空的上限
IDLE_TIMEOUT = 20.0


class BatchSender:
    def __init__(self, host, limit=DEFAULT_LIMIT, pace=DEFAULT_PACE,
                 settle_before_stop=SETTLE_BEFORE_STOP, dry_run=False,
                 log=None, sleep=time.sleep, clock=time.time,
                 reply_timeout=25.0, done_text='我已发送完毕'):
        self.host = host
        self.limit = max(1, int(limit or DEFAULT_LIMIT))
        self.pace = pace
        self.settle = settle_before_stop
        self.dry_run = dry_run
        # 回复超过这个秒数还没自然说完就截断（新协议下正常回复只有一行，1~3 秒）
        self.reply_timeout = float(reply_timeout or 25.0)
        # 最后一批要附带发出去的那句话（用户要求由工具带上）
        self.done_text = done_text
        self._log = log or (lambda *_a, **_k: None)
        self._sleep = sleep
        self._clock = clock
        self.cancelled = False

    # ── 对内小工具 ──

    def log(self, msg):
        try:
            self._log(msg)
        except Exception:  # noqa: BLE001
            pass

    def _cancel(self, should_cancel):
        if self.cancelled:
            return True
        return bool(should_cancel and should_cancel())

    def _wait_until(self, pred, timeout, interval=0.5, should_cancel=None):
        """轮询到 pred(state) 为真；超时返回 False。"""
        end = self._clock() + timeout
        while self._clock() < end:
            if self._cancel(should_cancel):
                return False
            st = self.host.state()
            if not st.get('ok'):
                self._sleep(interval)
                continue
            try:
                if pred(st):
                    return True
            except Exception:  # noqa: BLE001
                pass
            self._sleep(interval)
        return False

    def _ensure_idle(self, should_cancel):
        """确保页面不在生成中、且没有残留附件 —— 否则挂不上新文件/会超上限。"""
        st = self.host.state()
        if st.get('streaming'):
            self.log('检测到还在生成，先截断')
            self.host.stop()
            self._wait_until(lambda s: not s.get('streaming'), IDLE_TIMEOUT, should_cancel=should_cancel)
        n = int((self.host.state() or {}).get('attachments') or 0)
        if n:
            self.log(f'输入区还挂着 {n} 个附件（上一次的没发出去/没删掉），先等它清空')
            ok = self._wait_until(lambda s: not int(s.get('attachments') or 0),
                                  IDLE_TIMEOUT, should_cancel=should_cancel)
            if not ok:
                return (f'输入区还挂着 {n} 个没发出去的附件，已停下。'
                        f'这样做是为了避免一次挂超过 {self.limit} 个文件。'
                        f'请到内嵌页面上把这些附件发出去或点掉，再重新点「开始发送」')
        return ''

    # ── 主流程 ──

    def run(self, batches, on_progress=None, should_cancel=None):
        """按批发送。batches 是 plan.plan_batches() 的输出。

        返回结果 dict：
            ok            是否全部批次都发出去且没有致命错误
            batches       总批次数
            sent_batches  成功的批次数
            sent_files    成功发出的文件数
            failed        失败批次列表 [{'index','error','files'}]
            stopped       执行了几次"截断"
            cancelled     是否被用户取消
            error         致命错误（有值就直接结束）
        """
        batches = [list(b) for b in (batches or []) if b]
        total = len(batches)
        res = {'ok': True, 'batches': total, 'sent_batches': 0, 'sent_files': 0,
               'failed': [], 'stopped': 0, 'cancelled': False, 'error': ''}
        if not total:
            res['ok'] = False
            res['error'] = '没有要发送的文件'
            return res
        t_start = self._clock()

        for i, batch in enumerate(batches, 1):
            if self._cancel(should_cancel):
                res['cancelled'] = True
                res['ok'] = False
                break
            last = (i == total)
            # 进度里带上"预计还需多久"：按已用时间/已完成批次数推算，
            # 自己会越算越准（第一批慢是正常的，后面会收敛）。
            elapsed = self._clock() - t_start
            if i > 1:
                avg = elapsed / (i - 1)
                eta = fmt_dur(avg * (total - i + 1))
            else:
                eta = '估算中…'
            if on_progress:
                try:
                    on_progress(i - 1, total,
                                f'第 {i}/{total} 批（本批 {len(batch)} 个）· '
                                f'预计还需 {eta}')
                except Exception:  # noqa: BLE001
                    pass

            if self.dry_run:
                self.log(f'[演练] 第 {i}/{total} 批：{len(batch)} 个文件，'
                         f'{batch[0] if batch else ""} …')
                res['sent_batches'] += 1
                res['sent_files'] += len(batch)
                continue

            err = self._ensure_idle(should_cancel)
            if err:
                res['ok'] = False
                res['error'] = err
                break

            self.log(f'第 {i}/{total} 批：挂 {len(batch)} 个文件…')
            att = self._attach_with_retry(batch, should_cancel)
            if not att.get('ok') and not int(att.get('attached') or 0):
                why = att.get('error') or att.get('diag') or '未知原因'
                res['failed'].append({'index': i, 'error': why, 'files': batch})
                res['ok'] = False
                self.log(f'  挂附件失败：{why}')
                if i == 1:
                    res['error'] = f'第一批就挂不上附件：{why}'
                    break
                continue
            if not att.get('ok'):
                if att.get('sendReady') is False:
                    self.log('  附件挂上了，但发送键一直没变成可用（站点还在处理文件），'
                             '这一批可能发不出去')
                else:
                    self.log(f'  只挂上 {att.get("attached")}/{len(batch)} 个，'
                             f'仍继续发送这一批')
            if att.get('sendReady') is not None and att.get('sendReady') is False:
                self.log('  （send 内部还会再等一次发送键，最多 60 秒）')

            # 最后一批：把「我已发送完毕」**附在这条消息里**一起发出去
            # （用户要求：由工具在最后一批带上，而不是让他自己再发一条）。
            snd = self.host.send(text=self.done_text if last else '')
            if not snd.get('ok'):
                snd = self.host.send('enter')
            if not snd.get('ok'):
                why = snd.get('reason') or '发送键一直不可用（按钮和回车都没成功）'
                res['failed'].append({'index': i, 'error': why, 'files': batch})
                res['ok'] = False
                self.log(f'  发送失败：{why}')
                # 站点级失败（例如"服务器繁忙/请删除异常文件"）几乎都是**附件太多**：
                # 继续发下一批只会一路失败，所以直接停下并给出可执行的建议。
                if any(k in why for k in ('官网提示发送失败', '异常文件', '服务器繁忙',
                                          '请检查网络')):
                    res['error'] = (f'{why}\n'
                                    f'这通常是**一批文件太多**导致的：实测官网单条消息'
                                    f'接不下 40 个附件（30 个以内正常）。'
                                    f'请把左下角「每批 N 个」点一下调小再试。')
                    break
                if i == 1:
                    break
                continue

            res['sent_batches'] += 1
            res['sent_files'] += len(batch)
            self.log(f'  已发出（{snd.get("how") or "auto"}）'
                     + ('，已附带「我已发送完毕」' if last and self.done_text else ''))

            if last:
                # 最后一批要让它把话说完（它收到「我已发送完毕」后开始正式分析）
                self.log('最后一批：不截断，让它正常回答')
                break

            # ⚠️ 不再一上来就截断。
            # 用户反馈：被截断的消息在官网侧是"没说完"的状态，**不利于参与上下文**。
            # 新协议下模型每批只回一句「我已接收上述信息」（1~3 秒就说完），
            # 所以先等它**自然说完**；只有超过 reply_timeout 还没完（说明它没按协议
            # 走、开始长篇分析了）才截断，避免阻塞后面的批次。
            started = self._wait_until(lambda s: s.get('streaming'),
                                       STREAM_START_TIMEOUT, should_cancel=should_cancel)
            if not started:
                self.log('  没检测到"正在生成"（可能已经答完），继续下一批')
                self._pace_sleep()
                if on_progress:
                    try:
                        on_progress(i, total, f'已发 {i}/{total} 批')
                    except Exception:  # noqa: BLE001
                        pass
                continue
            finished = self._wait_until(lambda s: not s.get('streaming'),
                                        self.reply_timeout, interval=0.5,
                                        should_cancel=should_cancel)
            if finished:
                # 自然答完：这条回复完整进入上下文，不做任何截断。
                self.log('  回复已自然说完（未截断，完整进入上下文）')
            else:
                self._sleep(self.settle)
                self.log(f'  回复超过 {self.reply_timeout:.0f} 秒还没结束，截断它'
                         f'（否则会一直占着输入区、拖住后面的批次）')
                stp = self.host.stop()
                # how == 'none' 表示"本来就没在生成"，Electron 侧按契约也返回 stopped=true。
                # 这种不算真截断，否则日志里的"截断 N 次"会虚报。
                if stp.get('stopped') and (stp.get('how') or '') != 'none':
                    res['stopped'] += 1
                elif not stp.get('stopped'):
                    self.log(f'  截断没成功：{stp.get("diag") or stp.get("error") or stp}')
                self._wait_until(lambda s: not s.get('streaming'), IDLE_TIMEOUT,
                                 should_cancel=should_cancel)

            self._pace_sleep()

            if on_progress:
                try:
                    on_progress(i, total, f'已发 {i}/{total} 批')
                except Exception:  # noqa: BLE001
                    pass

        if on_progress:
            try:
                on_progress(res['sent_batches'], total,
                            f'完成 {res["sent_batches"]}/{total} 批')
            except Exception:  # noqa: BLE001
                pass
        return res

    def _pace_sleep(self):
        """两批之间的间隔（随机化一点，别像机器一样精确）。"""
        if not self.pace:
            return
        lo, hi = self.pace
        wait = random.uniform(lo, hi) if hi > lo else float(lo)
        self.log(f'  等 {wait:.1f} 秒再发下一批')
        self._sleep(wait)

    def _attach_with_retry(self, batch, should_cancel, tries=2):
        """挂附件。

        ⚠️ 只在**一个都没挂上**时才重试：部分挂上（例如 46/50）再注入一次，
        有可能把已挂的又叠一遍（官网对同名文件的处理不一致），得不偿失。
        部分挂上属于"可以继续"的情况，把差额如实打进日志就好。

        等待时间按文件类型给：文档 0.5 秒/个、图片 0.3 秒/个（用户定）。
        """
        last = {'ok': False, 'error': '未执行'}
        settle = settle_ms_for(batch)
        for k in range(tries):
            if self._cancel(should_cancel):
                return {'ok': False, 'error': '已取消'}
            last = self.host.attach(batch, settle_ms=settle)
            got = int(last.get('attached') or 0)
            if last.get('ok'):
                if got < len(batch):
                    self.log(f'  注意：页面只数到 {got}/{len(batch)} 个附件'
                             f'（{last.get("diag") or ""}）—— 继续发送')
                return last
            if got > 0:
                # 部分挂上：不再重试，交给上层继续
                return last
            self.log(f'  一个都没挂上：{last.get("error") or last.get("diag") or last}')
            if k + 1 < tries:
                self._sleep(1.5)
        return last

    def cancel(self):
        """外部（比如关闭进度窗）调用，让下一轮循环退出。"""
        self.cancelled = True
