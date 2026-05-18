"""
스케줄러: 정해진 시간마다 crawler.py를 자동 실행
기본 스케줄: 매일 08:00, 12:00, 18:00, 22:00 (KST)

사용법:
  python scheduler.py            # 스케줄러 시작 (포그라운드 실행)
  python scheduler.py --once     # 즉시 1회 실행 후 종료
"""

import schedule, time, sys, subprocess
from datetime import datetime
from pathlib import Path

CRAWLER = Path(__file__).parent / "crawler.py"
PYTHON  = sys.executable

# 매일 실행할 시각 (24시 기준)
RUN_TIMES = ["09:00", "12:00", "15:00", "18:00", "21:00"]


def run_crawler():
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 크롤러 실행 시작...")
    result = subprocess.run(
        [PYTHON, str(CRAWLER)],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"⚠️  크롤러 종료 코드: {result.returncode}")
    else:
        print(f"✅ 크롤러 정상 완료")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_crawler()
        sys.exit(0)

    print("스케줄러 시작")
    print(f"실행 시각: {', '.join(RUN_TIMES)}")
    print("종료하려면 Ctrl+C\n")

    for t in RUN_TIMES:
        schedule.every().day.at(t).do(run_crawler)

    while True:
        schedule.run_pending()
        time.sleep(30)
