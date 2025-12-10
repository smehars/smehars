import requests
import os
import hashlib
from lxml import etree

USER_NAME = "smehars"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {'Authorization': 'token ' + ACCESS_TOKEN}
OWNER_ID = None

def run_query(func_name, query, variables):
  """
  executes a query against the github graphql api
  """
  response = requests.post(
    GRAPHQL_URL,
    json={'query': query, 'variables': variables},
    headers=HEADERS
  )
  if response.status_code == 200:
    return response
  raise Exception(f'{func_name} failed with a {response.status_code}: {response.text}')

def graph_commits(start_date, end_date):
  """
  fetches my commit count using the GitHub API    
  """
  query = '''
  query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
    user(login: $login){
      contributionsCollection(from: $start_date, to: $end_date) {
        contributionCalendar {
          totalContributions
        }
      }
    }
  }
  '''
  variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
  response = run_query("graph_commits", query, variables)
  return int(response.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def force_close_file(data, cache_comment):
  """forces the file to close, preserving whatever data was written to it"""
  filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
  with open(filename, 'w') as f:
    f.writelines(cache_comment)
    f.writelines(data)
  print('There was an error while writing to the cache file.', filename, 'has been updated with partial data.')

def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, additional_total, deletion_total, commits):
  """recursively calls recusrive_loc since graphql is limited to 100 commits at a time
  only adds the LOC value of commits authored by me"""
  global OWNER_ID
  for edge in history['edges']:
    if edge['node']['author']['user'] and edge['node']['author']['user']['id'] == OWNER_ID:
      commits+=1  
      additional_total += edge['node']['additions']
      deletion_total += edge['node']['deletions']
  if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
    return additional_total, deletion_total, commits
  else:
    return recursive_loc(owner, repo_name, data, cache_comment, additional_total, deletion_total, commits, history['pageInfo']['endCursor'])

def recursive_loc(owner, repo_name, data, cache_comment, additional_total=0, deletion_total=0, commits=0, cursor=None):
  """Uses  GraphQL and cursor pagination to fetch 100 commits from a repository at a time"""
  global OWNER_ID
  query = '''
  query($repo_name: String!, $owner: String!, $cursor: String){
    repository(name: $repo_name, owner: $owner){
      defaultBranchRef {
        target{ 
          ... on Commit{
            history(first: 100, after: $cursor){
              totalCount
              edges{
                node{
                  ... on Commit{
                    committedDate
                  }
                  author{
                    user{
                      id
                    }
                  }
                  deletions
                  additions
                }
              }
              pageInfo{
                endCursor
                hasNextPage
              }
            }
          }
        }
      }
    }
  }'''
  variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
  response = requests.post(GRAPHQL_URL, json={'query': query, 'variables': variables}, headers=HEADERS)
  if response.status_code == 200:
    if response.json()['data']['repository']['defaultBranchRef'] is not None:
      return loc_counter_one_repo(owner, repo_name, data, cache_comment, response.json()['data']['repository']['defaultBranchRef']['target']['history'], additional_total, deletion_total, commits)
    else: 
      return 0, 0, 0
  force_close_file(data, cache_comment)
  if response.status_code == 403:
    raise Exception('too many requests')
  raise Exception(f'recursive_loc failed with a {response.status_code}: {response.text}')

def flush_cache(edges, filename, comment_size):
  """wipes the cache file. called when the number of repositories changes or when the file is created for the first time"""
  data = []
  try:
    with open(filename, 'r') as f:
      if comment_size > 0:
        data = f.readlines()[:comment_size]
  except FileNotFoundError:
    pass
  with open(filename, 'w') as f:
    f.writelines(data)
    for node in edges:
      f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
  """checks each repository in edges to see if it has been updated since the last time it was cached
  if it has been cached, run recursive_loc on that repository to update the LOC count
  """
  global OWNER_ID
  filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
  try:
    with open(filename, 'r') as f:
      data = f.readlines()
  except FileNotFoundError:
    data = []
    if comment_size >0:
      for _ in range(comment_size):
        data.append('comment block\n')
    with open(filename, 'w') as f:
      f.writelines(data)
  if len(data) - comment_size != len(edges) or force_cache:
    flush_cache(edges, filename, comment_size)
    with open(filename, 'r') as f:
      data = f.readlines()
  cache_comment = data[:comment_size]
  data = data[comment_size:]
  for i in range(len(edges)):
    repo_hash, commit_count, *__ = data[i].split()
    if repo_hash == hashlib.sha256(edges[i]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
      try:
        if int(commit_count) != edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']:
          owner, repo_name = edges[i]['node']['nameWithOwner'].split('/')
          loc = recursive_loc(owner, repo_name, data, cache_comment)
          data[i] = repo_hash + ' ' + str(edges[i]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
      except TypeError:
        data[i] = repo_hash + ' 0 0 0 0\n'
  with open(filename, 'w') as f:
    f.writelines(cache_comment)
    f.writelines(data)
  for line in data:
    loc = line.split()
    loc_add += int(loc[3])
    loc_del += int(loc[4])
  return [loc_add, loc_del, loc_add - loc_del]

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
  """uses graphQL api to query all repositories I have access to
  queries 50 repositories at a time using cursor pagination
  """
  if edges is None:
    edges = []
  query = '''
  query($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String){
    user(login: $login){
      repositories(first: 50, after: $cursor, ownerAffiliations: $owner_affiliation){
        edges{
          node{
            ... on Repository{
              nameWithOwner
              defaultBranchRef{
                target{
                  ... on Commit{
                    history{
                      totalCount
                    }
                  }
                }
              }
            }
          }
        }
        pageInfo{
          endCursor
          hasNextPage
        }
      }
    }
  }'''
  variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
  response = run_query("loc_query", query, variables)
  data = response.json()['data']['user']['repositories']
  if data['pageInfo']['hasNextPage']:
    edges += data['edges']
    return loc_query(owner_affiliation, comment_size, force_cache, data['pageInfo']['endCursor'], edges)
  else:
    return cache_builder(edges + data['edges'], comment_size, force_cache)

def get_contrib_count():
  """fetches the number of repositories the user has contributed to"""
  query = '''
  query($login: String!) {
    user(login: $login) {
      repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST]) {
        totalCount
      }
    }
  }
  '''
  variables = {'login': USER_NAME}
  response = run_query("get_contrib_count", query, variables)
  return response.json()['data']['user']['repositoriesContributedTo']['totalCount']

def get_repo_count():
  """fetches the number of public repositories the user owns"""
  query = '''
  query($login: String!) {
    user(login: $login) {
      repositories(ownerAffiliations: OWNER) {
        totalCount
      }
    }
  }
  '''
  variables = {'login': USER_NAME}
  response = run_query("get_repo_count", query, variables)
  return response.json()['data']['user']['repositories']['totalCount']

def find_and_replace(root, element_id, new_text):
  """finds an element by its ID in SVG file and replaces its text with new_text"""
  element = root.find(f".//*[@id='{element_id}']")
  if element is not None:
    element.text = str(new_text)
    print(f"  Updated {element_id} to: {new_text}")
    return True
  else:
    print(f"  WARNING: Element with id '{element_id}' not found!")
    return False

def svg_format(root, element_id, new_text, length=0):
  """updates and formats text of elements. modifies the amount of dots in between key and value to align everything"""
  if isinstance(new_text, int):
    new_text = f"{new_text:,}"
  new_text = str(new_text)
  find_and_replace(root, element_id, new_text)
  just_len = max(0, length - len(new_text))
  if just_len <= 2:
    dot_map = {0: '', 1: ' ', 2: '. '}
    dot_string = dot_map.get(just_len, '')
  else:
    dot_string = ' ' + ('.' * just_len) + ' '
  find_and_replace(root, f"{element_id}_dots", dot_string)

def svg_overwriter(filename, commit_data, loc_data):
  """parse svg files and updates their elements with latest stats"""
  print(f"\nUpdating {filename}...")
  print(f"  Commit data: {commit_data}")
  print(f"  LOC data: add={loc_data[0]}, del={loc_data[1]}, total={loc_data[2]}")
  
  parser = etree.XMLParser(remove_blank_text=False)
  tree = etree.parse(filename, parser)
  root = tree.getroot()
  svg_format(root, 'commit_data', commit_data, 0)
  svg_format(root, 'loc_data', loc_data[2], 0)
  svg_format(root, 'loc_add', loc_data[0])
  svg_format(root, 'loc_del', loc_data[1], 0)
  tree.write(filename, encoding='utf-8', xml_declaration=True)
  print(f"  Wrote changes to {filename}")
  
def commit_counter(comment_size):
  """counts my total commits, using cache file"""
  total_commits = 0
  filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
  with open(filename, 'r') as f:
    data = f.readlines()
  data = data[comment_size:]
  for line in data:
    total_commits += int(line.split()[2])
  return total_commits

def user_getter(username):
  """returns the account id and creation time of the username provided"""
  query = '''
  query($login: String!){
    user(login: $login){
      id
      createdAt
    }
  }
  '''
  variables = {'login': username}
  response = run_query("user_getter", query, variables)
  return response.json()['data']['user']['id'], response.json()['data']['user']['createdAt']

if __name__ == "__main__":
  OWNER_ID, created_at = user_getter(USER_NAME)
  loc_data = loc_query(['OWNER', 'COLLABORATOR'])
  commit_count = commit_counter(0)
  svg_overwriter('svg/light_stats.svg', commit_count, loc_data)
  svg_overwriter('svg/dark_stats.svg', commit_count, loc_data)