# Standard lib imports
import os
import re
import pdb
import logging
import csv
import json
from collections import defaultdict

# Third-party imports
import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# Local imports
from update_map import update_map

# Constants
DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(DIR))
CONTEST_URL = r'http://results.enr.clarityelections.com/GA/58980/163369/en/md_data.html?cid=51&'
COUNTIES = ['CLAYTON', 'FULTON', 'GWINNETT', 'DEKALB', 'COBB']
CANDIDATES = {'rep': 'DONALD J. TRUMP', 'dem': 'TED CRUZ'} # For testing w 2016 republican primary data

# Configure logging
logging.basicConfig(level=logging.INFO)

class Parser(object):
    """
    Base class that provides scraping functionality for Clarity Elections site.
    Use Selenium's PhantomJS headless browser to simulate clicks and get URL of detail
    pages for given counties, then gets precinct-level vote data for a given race.
    """

    def __init__(self, contest_url):
        self.main_url = contest_url

        # These instance variables will be set by the user
        self.county_urls = []
        self.precinct_results = []
        self.unmerged_precincts = None
        self.merged_precincts = None

    def _build_driver(self):
        """
        Create an instance of Selenium's webdriver.PhantomJS(), used to 
        simulate clicks on the Clarity elections site
        """
        driver = webdriver.PhantomJS()
        driver.get(self.main_url)
        assert 'Election' in driver.title # Make sure we have the right page
        return driver

    def get_county_urls(self, input_counties=None, delay=10):
        """
        Use Selenium to get the dynamically generated URLs for each county's 
        detail page, and append the URLs to self.county_urls.
        """
        self.county_urls = [] # Reset county URLs
        logging.info('Creating Selenium driver and accessing Clarity')
        driver = self._build_driver()

        try:
            string_counties = (', ').join(input_counties)
        except TypeError: 
            string_counties = 'All counties'

        logging.info('Getting detail page URLs for {}'.format(string_counties))

        # Get a list of all counties on the contest summary page
        selector = 'table.vts-data > tbody > tr > td.alignLeft:not(.total)'
        num_counties = len(driver.find_elements_by_css_selector(selector)) - 1

        # Have to do this instead of looping through county objects because
        # it will throw a StaleElementReferenceException
        for i in range(num_counties):
            # Get links from each county row
            county = driver.find_elements_by_css_selector(selector)[i]
            links = county.find_elements_by_tag_name('a')
            county_name = links[0].get_attribute('id')

            # Skip counties not in the list supplied by the user. If no list 
            # is provided then loop through all the counties
            if input_counties is not None and county_name.upper() not in input_counties:
                continue

            # The URL for each county is generated by Clarity on each page visit
            # Emulating a click is a sure bet to get to the detail page
            links[1].click()

            # Wait until the new page loads
            try:
                check = EC.presence_of_element_located((By.ID, 'precinctDetailLabel'))
                WebDriverWait(driver, delay).until(check)
            except TimeoutException:
                print 'Page took too long to load'

            # Remove cruft at the end of URL and append it to our list of URLs
            split_url = driver.current_url.split('/')
            base_url = ('/').join(split_url[:-2])
            self.county_urls.append([county_name.upper(), base_url])


            # Navigate back to the contest's home page
            driver.get(self.main_url)

        driver.close()
        return

    def get_precincts(self):
        """
        Get JSON data from the endpoints listed in :county_urls: and parse
        the precinct-level election results from each one
        """
        self.precinct_results = []
        for county_name, base_url in self.county_urls:
            logging.info('Getting precinct details from {}'.format(base_url))
            candidate_data = requests.get(base_url + '/json/sum.json')
            vote_data = requests.get(base_url + '/json/details.json')

            # Get a list of candidates and append it to the list of headers
            contests = json.loads(candidate_data.content)['Contests']
            # Find out which of the contests contains the candidates we're interested in.
            # Clarity changes the order of contests in the JSON files in multi-contest
            # elections.
            order = [i for i, val in enumerate(contests) if CANDIDATES['rep'] in val['CH']][0]
            candidates = contests[order]['CH']

            #Get votes for each candidate
            contests = json.loads(vote_data.content)['Contests']
            contest = contests[order]

            for precinct, votes in zip(contest['P'], contest['V']):
                data = {'precinct': precinct, 'county': county_name}
                total = 0
                for candidate, count in zip(candidates, votes):
                    if candidate == CANDIDATES['rep']:
                        total += float(count)
                        data['rep_votes'] = int(count)
                    elif candidate == CANDIDATES['dem']:
                        total += float(count)
                        data['dem_votes'] = int(count)
                    else:
                        total += float(count)
                #data['total'] = sum(votes)
                data['total'] = total

                self.precinct_results.append(data)

class ResultSnapshot(Parser):
    """
    Class that contains utilities for cleaning Georgia election results and
    merging with statistical data gathered from the US Census.
    """

    def __init__(self, **kwargs):
        super(ResultSnapshot, self).__init__(**kwargs)

    def _clean(self, row):
        """
        Private method forrenaming up the few precincts scraped from the site that
        have names that don't match the map names, when the map names can't be changed
        """
        r = re.compile(r'\d{3} ')
        precinct1 = re.sub(r, '', row['precinct'])
        precinct2 = re.sub(re.compile(r'EP04-05|EP04-13'), 'EP04', precinct1)
        precinct3 = re.sub(re.compile(r'10H1|10H2'), '10H', precinct2)
        precinct4 = re.sub(re.compile(r'CATES D - 04|CATES D - 07'), 'CATES D', precinct3)
        precinct5 = re.sub(re.compile(r'AVONDALE HIGH - 05|AVONDALE HIGH - 04'), 'AVONDALE HIGH', precinct4)
        precinct6 = re.sub(re.compile(r'CHAMBLEE 2'), 'CHAMBLEE', precinct5)
        precinct7 = re.sub(re.compile(r'WADSWORTH ELEM - 04'), 'WADSWORTH ELEM', precinct6)
        return precinct6.strip().upper()[:20]

    def _get_income(self, row):
        if row['avg_income'] < 50000:
            return 'low'
        elif row['avg_income'] < 100000:
            return 'mid'
        else:
            return 'high'

    def _get_rep_proportion(self, row):
        try:
            return float(row['rep_votes'])/row['total']
        except ZeroDivisionError:
            return 0

    def _get_dem_proportion(self, row):
        try:
            return float(row['dem_votes'])/row['total']
        except ZeroDivisionError:
            return 0


    def _clean_vote_stats(self, precincts):
        """
        Private method used to calculate proportions of voters for each 
        candidate by precinct, clean the precinct name, put the income in bins,
        and perform other operations necessary before it's ready to be 
        consumed by the JS app
        """
        cframe = precincts

        # Calculate proportion of total votes that each candidate got
        cframe['rep_p'] = cframe.apply(self._get_rep_proportion, axis=1)
        cframe['dem_p'] = cframe.apply(self._get_dem_proportion, axis=1)
        cframe['precinct'] = cframe.apply(self._clean, axis=1)

        return cframe

    def _get_income(self, row):
        if row['avg_income'] < 50000:
            return 'low'
        elif row['avg_income'] < 100000:
            return 'mid'
        else:
            return 'high'

    def merge_votes(self, statsf='ajc_precincts_merged.csv'):
        """
        Public method used to merge the election result dataset with the precinct 
        maps from the Reapportionment office.
        """
        votes = self.precinct_results
        votes = pd.DataFrame(votes)
        stats = pd.read_csv(statsf, index_col=False)

        fvotes = self._clean_vote_stats(votes)

        merged = stats.merge(fvotes,
            left_on='ajc_precinct',
            right_on='precinct',
            how='outer',
            indicator=True)

        # Drop null values
        merged = merged[pd.notnull(merged['rep_votes'])]
        merged = merged[pd.notnull(merged['dem_votes'])]
        pdb.set_trace()

        self.unmerged_precincts = merged[merged._merge != 'both']
        self.merged_precincts = merged[merged._merge == 'both']

        path = os.path.join(DIR, 'vote_data.csv')

        logging.info('Writing precinct information to csv {}'.format(path))
        self.merged_precincts.to_csv(path)
        return

    def aggregate_stats(self, statsfile='2014_precincts_income_race.csv'):
        """
        Calculate an aggregate stats file that's used to populate summary
        statistics in the map
        """
        just_votes = self.merged_precincts
        stats = pd.read_csv(statsfile)
        merged = just_votes.merge(stats, how='inner')
        merged['income_bin'] = merged.apply(self._get_income, axis=1)

        # Calculate aggregated stats for summary table
        race = merged.groupby(['county', 'race'])['rep_votes', 'dem_votes'].sum().unstack()
        income = merged.groupby(['county','income_bin'])['rep_votes', 'dem_votes'].sum().unstack()

        reps = race.rep_votes.merge(income.rep_votes, left_index=True, right_index=True)
        reps['party'] = 'rep_votes'
        repsf = reps.reset_index()

        dems = race.dem_votes.merge(income.dem_votes, left_index=True, right_index=True)
        dems['party'] = 'dem_votes'
        demsf = dems.reset_index()

        c = pd.concat([repsf, demsf])

        # Create a nested defaultdict
        data = defaultdict(lambda: defaultdict(dict))

        fields = ['black', 
                  'white',
                  'hispanic',
                  'high',
                  'mid',
                  'low']

        # Create a nested JSON object
        for i, row in c.iterrows():
            county = row['county']
            party = row['party']
            data[county]['all'][party] = 0

            for field in fields:
                # Check if val is null for precincts missing a certain group
                # (eg some precincts have no Hispanics)
                if pd.isnull(row[field]):
                    continue
                data[county][field][party] = row[field]
                data[county]['all'][party] += row[field]
                # It's impossible to use default dict for the below, because the factory can't
                # generate both dicts and ints by default
                try: 
                    data['ALL COUNTIES'][field][party] += row[field]
                except KeyError:
                    data['ALL COUNTIES'][field][party] = 0

        # Lastly, calculate summary stats for counties
        data['ALL COUNTIES']['all']['rep_votes'] = merged['rep_votes'].sum()
        data['ALL COUNTIES']['all']['dem_votes'] = merged['dem_votes'].sum()

        path = os.path.join(BASE_DIR, 'assets', 'data', '2014agg_stats')
        logging.info('Writing aggregated stats to {}'.format(path))

        with open(path, 'w') as f:
            f.write(json.dumps(data, indent=4))

        return


if __name__ == '__main__':
    p = ResultSnapshot(contest_url=CONTEST_URL)
    p.get_county_urls(input_counties=COUNTIES)
    p.get_precincts()
    p.merge_votes()
    p.aggregate_stats()
    update_map()

