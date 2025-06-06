import sys
import os
import json
import requests
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QLabel
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont

from bs4 import BeautifulSoup  # pip install beautifulsoup4


class WorkerSignals(QObject):
    progress = Signal(str)  # текст статуса


def parse_farming_simulator_mod(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/58.0.3029.110 Safari/537.3"
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Название
        title_tag = soup.find("div", class_="modtitle")
        if not title_tag:
            # Альтернативный способ: ищем h1 или title
            title_tag = soup.find("h1")
        name = title_tag.text.strip() if title_tag else "N/A"

        # Версия и дата
        version = "N/A"
        released = "N/A"
        for info in soup.find_all("div", class_="modinfo"):
            text = info.get_text(separator="\n")
            for line in text.splitlines():
                if "Version" in line:
                    version = line.split("Version")[-1].strip()
                if "Released" in line:
                    released = line.split("Released")[-1].strip()

        # Прямая ссылка на архив
        zip_url = None
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.lower().endswith(".zip"):
                zip_url = href
                break
        if zip_url and zip_url.startswith("/"):
            zip_url = "https://www.farming-simulator.com" + zip_url

        return {
            "name": name,
            "version": version,
            "date": released,
            "asset_url": zip_url,
            "asset_name": zip_url.split("/")[-1] if zip_url else None
        }
    except Exception as e:
        print("Ошибка парсинга farming-simulator:", e)
        return {
            "name": "Ошибка парсинга",
            "version": "N/A",
            "date": "N/A",
            "asset_url": None,
            "asset_name": None
        }


def validate_repo_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path_parts = parsed.path.strip("/").split("/")

        if netloc == "github.com":
            if len(path_parts) >= 2 and all(path_parts[:2]):
                return True
            return False

        elif netloc in ("www.farming-simulator.com", "farming-simulator.com"):
            if parsed.path == "/mod.php":
                params = parse_qs(parsed.query)
                if "mod_id" in params:
                    return True
            return False

        else:
            return False
    except Exception:
        return False


def download_file(url, save_path, signals):
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            total_length = int(r.headers.get('content-length', 0))
            downloaded = 0
            start_time = time.time()
            chunk_size = 8192

            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = int(downloaded * 100 / total_length) if total_length else 0

                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        speed_kb = speed / 1024

                        remaining_bytes = total_length - downloaded
                        eta = remaining_bytes / speed if speed > 0 else 0

                        progress_text = (
                            f"Скачано: {downloaded // 1024} KB / {total_length // 1024} KB | "
                            f"Скорость: {speed_kb:.2f} KB/s | "
                            f"Осталось: {int(eta)} сек | {percent}%"
                        )

                        signals.progress.emit(progress_text)

            signals.progress.emit("Загрузка завершена")
    except Exception as e:
        signals.progress.emit(f"Ошибка при скачивании: {str(e)}")
        if os.path.exists(save_path):
            os.remove(save_path)


class GitHubTrackerApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GitHub и Farming Simulator Tracker")
        self.resize(1000, 550)

        self.data_file = "repositories.json"
        self.tracked_repos = self.load_data()
        self.column_widths = self.tracked_repos.get("_column_widths", {})

        self.setup_ui()
        self.update_table()
        self.update_releases()  # Автообновление при старте

    def setup_ui(self):
        main_layout = QVBoxLayout(self)

        # Верхняя панель: поле ввода + кнопки Добавить и Удалить
        top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Введите ссылку на GitHub или Farming Simulator мод")
        top_layout.addWidget(self.url_input)

        self.btn_add = QPushButton("Добавить")
        self.btn_add.clicked.connect(self.add_repo_from_input)
        top_layout.addWidget(self.btn_add)

        self.btn_delete = QPushButton("Удалить выбранный")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self.delete_selected)
        top_layout.addWidget(self.btn_delete)

        main_layout.addLayout(top_layout)

        # Таблица с репозиториями
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Репозиторий",
            "Текущая версия",
            "Дата релиза",
            "Предыдущая версия",
            "Дата предыдущей",
            "Имя zip-файла",
            "Действия"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(False)

        for col in range(self.table.columnCount()):
            width = self.column_widths.get(str(col))
            if width:
                self.table.setColumnWidth(col, width)
            else:
                self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.table.horizontalHeader().sectionResized.connect(self.save_column_widths)
        self.table.selectionModel().selectionChanged.connect(self.on_selection_changed)

        main_layout.addWidget(self.table)

        # Нижняя панель: кнопка Обновить и статус под ней
        bottom_layout = QVBoxLayout()

        btn_layout = QHBoxLayout()
        self.btn_update = QPushButton("Обновить")
        self.btn_update.clicked.connect(self.update_releases)
        btn_layout.addWidget(self.btn_update)
        btn_layout.addStretch()
        bottom_layout.addLayout(btn_layout)

        self.status_label = QLabel("Готов")
        self.status_label.setMinimumHeight(20)
        bottom_layout.addWidget(self.status_label)

        main_layout.addLayout(bottom_layout)
        self.table.itemChanged.connect(self.on_item_changed)

    def on_item_changed(self, item):
        # Проверяем, что редактируется первый столбец
        if item.column() == 0:
            new_name = item.text()
            row = item.row()
            # Получаем URL репозитория из текущей строки (например, по индексу)
            # Для этого нужно хранить соответствие row -> url
            url = self.get_url_by_row(row)
            if url:
                # Обновляем поле name в last_release
                if "last_release" in self.tracked_repos[url]:
                    self.tracked_repos[url]["last_release"]["name"] = new_name
                    self.save_data()
                    self.status_label.setText(f"Название репозитория обновлено: {new_name}")

    def get_url_by_row(self, row):
        # Поскольку порядок строк в таблице совпадает с порядком в self.tracked_repos (без _column_widths),
        # можно получить URL так:
        keys = [k for k in self.tracked_repos.keys() if k != "_column_widths"]
        if 0 <= row < len(keys):
            return keys[row]
        return None

    def on_selection_changed(self):
        selected = self.table.selectionModel().hasSelection()
        self.btn_delete.setEnabled(selected)

    def save_column_widths(self):
        self.column_widths = {}
        for col in range(self.table.columnCount()):
            self.column_widths[str(col)] = self.table.columnWidth(col)
        self.tracked_repos["_column_widths"] = self.column_widths
        self.save_data()

    def add_repo_from_input(self):
        url = self.url_input.text().strip()
        if not url:
            self.status_label.setText("Введите ссылку на репозиторий или мод")
            return
        if not validate_repo_url(url):
            self.status_label.setText("Некорректный URL репозитория или мода")
            return
        if url in self.tracked_repos:
            self.status_label.setText("Репозиторий или мод уже отслеживается")
            return

        self.tracked_repos[url] = {"last_release": None, "previous_release": None}
        self.save_data()
        self.update_table()
        self.url_input.clear()
        self.status_label.setText("Репозиторий или мод добавлен")

    def delete_selected(self):
        selected_rows = set(idx.row() for idx in self.table.selectedIndexes())
        if not selected_rows:
            self.status_label.setText("Выберите репозиторий или мод для удаления")
            return

        urls_to_delete = []
        for row in selected_rows:
            repo_name = self.table.item(row, 0).text()
            for url in self.tracked_repos:
                if url == "_column_widths":
                    continue
                if "github.com" in url:
                    try:
                        owner, repo = self.get_owner_repo(url)
                    except IndexError:
                        continue  # Пропускаем некорректные ссылки
                    if repo == repo_name:
                        urls_to_delete.append(url)
                        break
                elif "farming-simulator.com" in url:
                    name = self.tracked_repos[url].get("last_release", {}).get("name", "")
                    if name == repo_name:
                        urls_to_delete.append(url)
                        break

        for url in urls_to_delete:
            self.tracked_repos.pop(url, None)
        self.save_data()
        self.update_table()
        self.status_label.setText("Выбранные репозитории или моды удалены")

    def update_releases(self):
        updated = False
        for url in list(self.tracked_repos.keys()):
            if url == "_column_widths":
                continue
            try:
                if "github.com" in url:
                    owner, repo = self.get_owner_repo(url)
                    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
                    response = requests.get(api_url, headers={"Accept": "application/vnd.github+json"})
                    if response.status_code == 200:
                        latest = response.json()
                        current = self.tracked_repos[url].get("last_release") or {}
                        new_version = latest.get("tag_name")
                        asset_url = None
                        asset_name = None
                        for asset in latest.get("assets", []):
                            if asset["name"].lower().endswith(".zip"):
                                asset_url = asset["browser_download_url"]
                                asset_name = asset["name"]
                                break
                        if new_version != current.get("version") or asset_url != current.get("asset_url"):
                            self.tracked_repos[url]["previous_release"] = current.copy()
                            self.tracked_repos[url]["last_release"] = {
                                "version": new_version,
                                "date": latest.get("published_at", ""),
                                "asset_url": asset_url,
                                "asset_name": asset_name,
                                "is_new": True,
                                "name": repo
                            }
                            updated = True
                        else:
                            self.tracked_repos[url]["last_release"]["is_new"] = False
                    else:
                        self.status_label.setText(f"Ошибка получения релизов для {owner}/{repo}")
                elif "farming-simulator.com" in url:
                    mod_info = parse_farming_simulator_mod(url)
                    current = self.tracked_repos[url].get("last_release") or {}
                    new_version = mod_info["version"]
                    asset_url = mod_info["asset_url"]
                    asset_name = mod_info["asset_name"]
                    if new_version != current.get("version") or asset_url != current.get("asset_url"):
                        self.tracked_repos[url]["previous_release"] = current.copy()
                        self.tracked_repos[url]["last_release"] = {
                            "version": new_version,
                            "date": mod_info["date"],
                            "asset_url": asset_url,
                            "asset_name": asset_name,
                            "is_new": True,
                            "name": mod_info["name"]
                        }
                        updated = True
                    else:
                        self.tracked_repos[url]["last_release"]["is_new"] = False
            except Exception as e:
                self.status_label.setText(f"Ошибка при обновлении {url}: {e}")

        self.save_data()
        self.update_table()
        if updated:
            self.status_label.setText("Найдены новые версии!")
        else:
            self.status_label.setText("Все репозитории и моды актуальны.")

    def format_release_date(self, date_str):
        if not date_str:
            return "N/A"
        try:
            dt = None
            # Попытка распарсить дату в разных форматах
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(date_str, fmt)
                    break
                except:
                    pass
            if dt is None:
                return date_str

            dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            days_ago = delta.days

            months = [
                "Января", "Февраля", "Марта", "Апреля", "Мая", "Июня",
                "Июля", "Августа", "Сентября", "Октября", "Ноября", "Декабря"
            ]
            day = dt.day
            month_name = months[dt.month - 1]
            date_formatted = f"{day} {month_name}"

            if days_ago == 0:
                days_text = "Сегодня"
            elif days_ago == 1:
                days_text = "1 день назад"
            elif 2 <= days_ago <= 4:
                days_text = f"{days_ago} дня назад"
            else:
                days_text = f"{days_ago} дней назад"

            return f"{date_formatted}\n<span style='font-size:small; color:gray;'>({days_text})</span>"
        except Exception:
            return date_str

    def update_table(self):
        self.table.setRowCount(0)
        for url, data in self.tracked_repos.items():
            if url == "_column_widths":
                continue
            release = data.get("last_release") or {}
            prev_release = data.get("previous_release") or {}

            row = self.table.rowCount()
            self.table.insertRow(row)



            # Имя репозитория или мода
            name = release.get("name", "N/A")
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(release.get("version", "N/A")))

            # Дата релиза с форматированием
            date_html = self.format_release_date(release.get("date", ""))
            label = QLabel()
            label.setTextFormat(Qt.RichText)
            label.setText(date_html)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setCellWidget(row, 2, label)

            self.table.setItem(row, 3, QTableWidgetItem(prev_release.get("version", "N/A")))
            self.table.setItem(row, 4, QTableWidgetItem((prev_release.get("date") or "N/A")[:10]))

            asset_name = release.get("asset_name", "N/A")
            link_item = QTableWidgetItem(asset_name)
            link_item.setFlags(link_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 5, link_item)

            btn_download = QPushButton("Скачать")
            btn_download.clicked.connect(lambda checked, u=url: self.download_release(u))
            self.table.setCellWidget(row, 6, btn_download)

            item = QTableWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsEditable)  # Разрешаем редактирование
            self.table.setItem(row, 0, item)

            if release.get("is_new", False):
                btn_download.setStyleSheet("background-color: yellow")
            else:
                btn_download.setStyleSheet("")

    def get_owner_repo(self, url):
        parts = urlparse(url).path.strip("/").split("/")
        return parts[0], parts[1]

    def load_data(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_data(self):
        self.tracked_repos["_column_widths"] = self.column_widths
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.tracked_repos, f, ensure_ascii=False, indent=2)

    def download_release(self, url):
        release = self.tracked_repos[url].get("last_release")
        if not release or not release.get("asset_url"):
            self.status_label.setText("Нет zip-файла для скачивания.")
            return

        asset_url = release["asset_url"]
        filename = release["asset_name"]
        save_dir = os.path.expandvars(r"%USERPROFILE%\Documents\My Games\FarmingSimulator2025\mods")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)

        self.status_label.setText(f"Начинается скачивание {filename}...")

        signals = WorkerSignals()
        signals.progress.connect(self.status_label.setText)

        def thread_func():
            download_file(asset_url, save_path, signals)

        threading.Thread(target=thread_func, daemon=True).start()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitHubTrackerApp()
    window.show()
    sys.exit(app.exec())
