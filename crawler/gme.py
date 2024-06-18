#!/usr/bin/env python3
# SPDX-FileCopyrightText: Florian Maurer, lucagioack@gmail.com
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Gestore Mercati Energetici (Italian Energy Markets Manager)

Inspired by and taken with permission from: https://github.com/lucagioacchini/electricity-market-maximizer
See also: https://ieeexplore.ieee.org/abstract/document/9209761
https://ieeexplore.ieee.org/abstract/document/9209787

Italian Market results for various markets.
The bidding results are available without anonymization.
"""

import logging
from pathlib import Path
from queue import Queue
from time import sleep
from zipfile import ZipFile

from selenium import webdriver
from selenium.webdriver.firefox.options import Options

RESTRICTION = "https://www.mercatoelettrico.org/It/Download/DownloadDati.aspx"
QUEUE = Queue()
DOWNLOAD = str(Path("./downloads").expanduser().absolute())

log = logging.getLogger(__name__)


class GMESpider:
    """Pass the license and agreements flags of the GME website, reach the all
    download fields, enter the starting and ending download period. Then
    extract all the .zip downloaded files and store their name in a queue from
    which they will be processed.

    Attributes
    ----------
        driver : selenium.webdriver.firefox.webdriver.WebDriver
            selenium instance creating a Firefox web driver
        log : logging.logger
            logger instalnce to display and save logs

    Methods
    -------
        passRestrictions()
        getData(gme, start, end)
        getFname(fname, start, end)
        checkDownload(fname)
        unzip(fname)
    """

    def __init__(self):
        # Set the firefox profile preferences

        profile = Options()
        profile.add_argument("--headless")

        profile.set_preference("browser.download.folderList", 2)
        profile.set_preference("browser.helperApps.alwaysAsk.force", False)
        profile.set_preference("browser.download.manager.showWhenStarting", False)
        profile.set_preference("browser.download.dir", DOWNLOAD)
        profile.set_preference("browser.download.downloadDir", DOWNLOAD)
        profile.set_preference("browser.download.defaultFolder", DOWNLOAD)
        profile.set_preference("browser.helperApps.neverAsk.saveToDisk", "text/html")
        profile.set_preference(
            "browser.helperApps.neverAsk.saveToDisk", "application/x-gzip"
        )

        # Class init
        self.driver = webdriver.Firefox(
            profile,
            # log_path='logs/geckodrivers.log'
        )

        self.driver.set_page_load_timeout(15)
        self.passRestrictions()

    def passRestrictions(self):
        """At the beginning of each session the GME website requires the flag
        and the submission of the Terms and Conditions agreement. Selenium
        emulates the user's click and passes these restrictions.
        """
        connected = False
        while not connected:
            try:
                self.driver.get(RESTRICTION)
                connected = True

            except Exception as e:
                log.error(f"[GME] connection failed. Trying again. {e}")
                sleep(5)

        # Flag the Agreement checkboxes
        _input = self.driver.find_element("id", "ContentPlaceHolder1_CBAccetto1")
        _input.click()
        _input = self.driver.find_element("id", "ContentPlaceHolder1_CBAccetto2")
        _input.click()
        # Submit the agreements
        _input = self.driver.find_element("id", "ContentPlaceHolder1_Button1")
        _input.click()
        log.info("[GME] Agreements passed")

    def getData(self, gme, start, *end):
        """Insert the starting and ending date of data and download them by
        emulating the user's click. After the download in the 'downloads/'
        folder, the file is checked. If the download failed, it is tried again.

        Parameters
        ----------
            gme : dict
                GME url to retrieve data and name of the downloaded file
                without extension
            start : str
                starting date data are refearred to
            *end : str
                ending date data are refearred to

        """
        downloaded = False

        while not downloaded:
            try:
                if len(end) > 0:
                    log.info(
                        "[GME] Retrieving data:"
                        f"\n\t{gme['fname']}\n\t{start} - {end[0]}"
                    )
                else:
                    log.info(
                        f"[GME] Retrieving data:\n\t{gme['fname']}\n\t{start}"
                    )
                self.driver.get(gme["url"])
                # Set the starting and endig date.
                # The GME has the one-month restriction
                _input = self.driver.find_element(
                    "id", "ContentPlaceHolder1_tbDataStart"
                )
                _input.send_keys(start)
                if len(end) > 0:
                    _input = self.driver.find_element(
                        "id", "ContentPlaceHolder1_tbDataStop"
                    )
                    _input.send_keys(end)

                # Download file
                _input = self.driver.find_element(
                    "id", "ContentPlaceHolder1_btnScarica"
                )
                _input.click()

                # Check if download succeded
                if len(end) > 0:
                    downloaded = self.checkDownload(
                        self.getFname(gme["fname"], start, end[0])
                    )
                else:
                    downloaded = self.checkDownload(self.getFname(gme["fname"], start))

            except Exception as e:
                log.warning(f"[GME] Trying again... {e}")
                sleep(5)

    def getFname(self, fname, start, *end):
        """Build the downloaded zipped file name on the basis of the starting
        and ending date.

        Parameters
        ----------
            fname : str
                file name without extension retrieved by the dict.
                in the config. file
            start : str
                starting date data are refearred to
            *end : str
                ending date data are refearred to

        Returns
        -------
            str
                zipped file name
        """
        dds, mms, yys = start.split("/")
        if len(end) > 0:
            dde, mme, yye = end[0].split("/")
            period = yys + mms + dds + yye + mme + dde
            fname += period
        else:
            period = yys + mms + dds
            fname = period + fname
        fname += ".zip"

        return fname

    def checkDownload(self, fname):
        """Check if the file has been downloaded into the 'downloads/' folder.

        Parameters
        ----------
            fname : str
                name of the downloaded file

        Returns
        -------
            bool
                True if the file has been downloaded, False otherwise
        """
        if Path(DOWNLOAD + "/" + fname).is_file():
            log.info("[GME] Zip file downloaded")
            self.unZip(fname)

            return True
        else:
            log.error(f"[GME] {fname} download failed")
            return False

    def unZip(self, fname):
        """Unzip the downloaded files and remove the zipped one from the folder.
        If the zipped files contains more zipped ones, those are extracted
        and processed too.

        Parameters
        ----------
            fname : str
                .zip file name
        """
        unzipped = False
        containzip = False
        while not unzipped:
            try:
                with ZipFile(DOWNLOAD + "/" + fname, "r") as zip:
                    zlist = zip.namelist()
                    if ".zip" in zlist[0]:
                        containzip = True
                    # extracting all the files
                    log.info("[GME] Extracting data...")
                    zip.extractall(DOWNLOAD)
                    log.info("[GME] Data extracted")

                Path(DOWNLOAD + "/" + fname).unlink()
                unzipped = True

                # Add the .xml files to the queue
                if not containzip:
                    [QUEUE.put(i) for i in zlist]

                # Remove the MPEG files
                for files in Path(DOWNLOAD).iterdir():
                    if "MPEG" in str(files):
                        files.unlink()

                # If the zip contains zipped files extract them
                if containzip:
                    for item in zlist:
                        if "MPEG" not in item:
                            self.unZip(item)

            except Exception:
                log.error(f"[GME] {fname} not found. Trying again...")
                sleep(1)


if __name__ == "__main__":
    import pandas as pd

    # !pip install lxml
    spid = GMESpider()
    from datetime import datetime

    start = datetime(2024, 1, 1)
    GME_WEEK = {
        "fname": "OfferteFree_Pubbliche",
        "url": "https://www.mercatoelettrico.org/it/Download/DownloadDati.aspx?val=OfferteFree_Pubbliche",
    }
    spid.getData(GME_WEEK, start.strftime("%d/%m/%Y"))

    df = pd.read_xml(
        DOWNLOAD + "/20240101AFRROffertePubbliche.xml"
    )  # , parse_dates=["BID_OFFER_DATE_DT"])
    df["BID_OFFER_DATE_DT"] = pd.to_datetime(df["BID_OFFER_DATE_DT"], format="%Y%m%d")
    df = df.drop(0, axis=0)
    df = df.dropna(how="all", axis=1)
    df.to_csv(DOWNLOAD + "/20240101AFRROffertePubbliche.csv")
