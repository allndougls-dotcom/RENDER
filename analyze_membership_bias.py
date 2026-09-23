from pathlib import Path
import pandas as pd

START = pd.Timestamp('2024-08-11')
END = pd.Timestamp('2026-09-10')

files = sorted(Path('data/master').glob('sp500_full_export_*.csv'))
if not files:
    raise SystemExit('No master export')
df = pd.read_csv(files[-1], usecols=lambda c: c in {'ticker','company','sector','date_added'})
df['date_added_parsed'] = pd.to_datetime(df.get('date_added'), errors='coerce')
future = df[(df['date_added_parsed'].notna()) & (df['date_added_parsed'] > START) & (df['date_added_parsed'] <= END)].copy()
future = future.sort_values('date_added_parsed')
print(f'Current constituents: {len(df)}')
print(f'Known date_added: {df.date_added_parsed.notna().sum()}')
print(f'Joined after backtest start and by end: {len(future)}')
show_cols = [c for c in ['ticker','company','sector','date_added_parsed'] if c in future.columns]
if len(future):
    print(future[show_cols].to_string(index=False))
future.to_csv('sidi_future_constituents_2024_2026.csv', index=False)
