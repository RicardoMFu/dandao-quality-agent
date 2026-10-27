#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python server.py
else
  print '请先按README安装Python 3.10以上版本并创建.venv环境。'
  read 'reply?按回车关闭'
fi
