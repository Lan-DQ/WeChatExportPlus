# -*- coding: utf-8 -*-
"""打包环境探针：把每个启动步骤写到文件，定位冻结版卡在哪。"""
import os
import sys
import traceback

LOG = r'C:\My_GongJu\grab\_probe\probe.log'


def step(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
            f.flush()
    except Exception:
        pass


open(LOG, 'w', encoding='utf-8').close()
step(f'frozen={getattr(sys, "frozen", False)}')
step(f'executable={sys.executable}')

try:
    step('1. import tkinter')
    import tkinter as tk
    step(f'   tkinter OK, TkVersion={tk.TkVersion}')

    step('2. import PIL')
    from PIL import Image, ImageDraw, ImageFilter
    step('   PIL OK')

    step('3. import PIL.ImageTk')
    from PIL import ImageTk
    step('   ImageTk OK')

    step('4. Tk()')
    r = tk.Tk()
    step('   Tk OK')

    step('5. geometry+update')
    r.geometry('300x200')
    r.update_idletasks()
    r.update()
    step(f'   update OK, winfo_id={r.winfo_id()}')

    step('6. 生成背景图')
    img = Image.new('RGB', (300, 200), (200, 210, 230))
    d = ImageDraw.Draw(img)
    d.ellipse([50, 50, 250, 150], fill=(120, 140, 220))
    img = img.filter(ImageFilter.GaussianBlur(8))
    step('   背景 OK')

    step('7. ImageTk.PhotoImage')
    ph = ImageTk.PhotoImage(img)
    step('   PhotoImage OK')

    step('8. Canvas + 贴图')
    cv = tk.Canvas(r, width=300, height=200, highlightthickness=0)
    cv.pack()
    cv.create_image(0, 0, image=ph, anchor='nw')
    step('   Canvas OK')

    step('9. 导入本项目的界面模块')
    sys.path.insert(0, os.path.join(os.path.dirname(sys.executable), 'gui'))
    import ui_theme
    step('   ui_theme OK')
    import ui_widgets
    step('   ui_widgets OK')

    step('10. 创建 Surface')
    th = ui_theme.THEMES['light']
    sf = ui_widgets.Surface(r, th)
    step('    Surface OK')
    sf.font = ui_theme.pick_font(r)
    step(f'    font={sf.font}')
    r.update_idletasks()
    r.update()
    sf.redraw_bg()
    step(f'    redraw_bg OK, canvas={sf.canvas.winfo_width()}x{sf.canvas.winfo_height()}')

    step('11. 创建一个按钮')
    b = ui_widgets.Button(sf, 20, 20, 120, 40, 'test', kind='primary')
    step(f'    按钮图元={len(sf.canvas.find_withtag(b.tags))}')

    step('12. mainloop（2 秒后自动退出）')
    r.after(2000, r.destroy)
    r.mainloop()
    step('    全部通过')

except Exception:
    step('EXCEPTION:\n' + traceback.format_exc())

step('=== END ===')
