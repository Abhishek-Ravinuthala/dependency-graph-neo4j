import requests
from bs4 import BeautifulSoup
from neo4j import GraphDatabase
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.requirements import Requirement
from packaging.version import Version, InvalidVersion
import sys
from dotenv import load_dotenv
import os

SpecifierDict = {}
def get_python_compatibility(package_name, version):
    response = requests.get(f"https://pypi.org/pypi/{package_name}/{version}/json")
    if response.status_code == 200:
        data = response.json()
        requires_python = data["info"].get("requires_python", None)
        
        if requires_python:
            try:
                # SpecifierDict[package_name] = SpecifierSet(requires_python)
                # print(SpecifierDict)
                # print(package_name, SpecifierSet(requires_python))
                return SpecifierSet(requires_python)
            except InvalidSpecifier:
                print(f"Invalid specifier for {package_name}=={version}: {requires_python}")
    return SpecifierSet()

def get_latest_compatible_version(package_name, python_version, upgrade):
    response = requests.get(f"https://pypi.org/pypi/{package_name}/json")
    if response.status_code == 200:
        data = response.json()
        releases = data.get("releases", {})
        sorted_versions = sorted(releases.keys(), key=lambda v: Version(v), reverse=not upgrade)
        
        for version in sorted_versions:
            specifier = get_python_compatibility(package_name, version)
            if Version(python_version) in specifier:
                return version
    return None

def read_requirements(file_path):
    with open(file_path, 'r') as file:
        lines = file.readlines()
    return [line.strip() for line in lines if line.strip()]

def find_compatible_python_version(requirements):
    python_specifiers = []
    incompatible_packages = []

    for req in requirements:
        try:
            req_obj = Requirement(req)
            package = req_obj.name
            version = str(req_obj.specifier).split(',')[0][2:]
            specifier = get_python_compatibility(package, version)
            if specifier:
                python_specifiers.append(specifier)
        except Exception as e:
            incompatible_packages.append(req)
            print(f"Failed to process {req}: {e}")

    if python_specifiers:
        common_specifier = python_specifiers[0]
        for specifier in python_specifiers[1:]:
            common_specifier &= specifier
    else:
        common_specifier = SpecifierSet()

    return common_specifier, incompatible_packages


# Function to fetch dependencies from PyPI API
def get_dependencies_from_pypi(package_name, version):
    try:
        if isinstance(version, list):
            version = version[0]
        # Extract the most appropriate version for the API call
        base_version = extract_base_version(version)

        if base_version:
            url = f"https://pypi.org/pypi/{package_name}/{base_version}/json"
        else:
            url = f"https://pypi.org/pypi/{package_name}/json"

        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        info = data.get("info", {})
        if not info:
            return {}

        dependencies = data.get("info", {}).get("requires_dist")
        if dependencies is None:
            return {}

        dependency_versions = {}

        for dep in dependencies:
            # Filter out optional dependencies
            if 'extra' not in dep:
                dep_name, dep_version = parse_dependency(dep)
                if dep_name:
                    dependency_versions[dep_name] = dep_version

        return dependency_versions

    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch dependencies for {package_name}=={version}: {e}")
        return {}

# Function to extract the most appropriate base version from the specifier
def extract_base_version(version):
    specifiers = version.split(',')
    for specifier in specifiers:
        if '>=' in specifier or '>' in specifier or '~=' in specifier:
            return specifier.lstrip('>=<~').strip()
    return specifiers[0].lstrip('>=<~').strip()

# Function to parse dependency and extract name and version
def parse_dependency(dep):
    dep = dep.split(';')[0].strip()
    parts = dep.split()
    if len(parts) >= 2:
        dep_name = parts[0].strip()
        dep_version = " ".join(parts[1:]).strip('()')
        return dep_name, dep_version
    elif len(parts) == 1:
        if '>=' in parts[0]:
            parts_new = parts[0].split('>=') 
            return parts_new[0].strip(), f'>={parts_new[1].strip()}'
        
        elif '<' in parts[0]:
            parts_new = parts[0].split('<') 
            return parts_new[0].strip(), f'<{parts_new[1].strip()}'
    else:
        return None, None

# Function to create graph in Neo4j
def create_graph(package_name, version, dependency_versions, driver):
    def add_or_update_package(tx, package_name, package_version):
        tx.run("""
            MERGE (p:Package {name: $name})
            ON CREATE SET p.version = $version
            ON MATCH SET p.version = $version
            """, name=package_name, version=package_version)
    def add_dependency(tx, root_package_name, root_version, dependency_name, dependency_version, relationship):
        tx.run("""
            MATCH (root:Package {name: $root_package_name})
            MERGE (dep:Package {name: $dependency_name})
            MERGE (root)-[:DEPENDS_ON {relationship: $relationship}]->(dep)
            """, root_package_name=root_package_name, dependency_name=dependency_name, relationship=relationship)

    def process_dependencies(tx, root_package_name, root_version, dependency_versions):
        existing_root_node = find_existing_package(tx, root_package_name)
        if existing_root_node:
            existing_root_version = existing_root_node['version']
            if existing_root_version!=root_version:
                valid, version = parse_version(root_version, existing_root_version, root_package_name)
                if valid:
                        add_or_update_package(tx, root_package_name, version)
                else:
                    add_or_update_package(tx, root_package_name, version)
                    sys.exit(f'Upgrade the package {root_package_name}, currently incompatible')

            
        else:
            add_or_update_package(tx, root_package_name, root_version)
        for dep_name, dep_version in dependency_versions.items():
            if dep_name != root_package_name:
                # Check if node already exists in Neo4j
                existing_node = find_existing_package(tx, dep_name)
                if existing_node:
                    existing_version = existing_node['version']
                    valid, version = parse_version(dep_version, existing_version, dep_name)
                    if valid:
                        add_or_update_package(tx, dep_name, version)
                        add_dependency(tx, root_package_name, root_version, dep_name, version, "DEPENDS_ON")
                        # Recursively process sub-dependencies
                        sub_dependencies = get_dependencies_from_pypi(dep_name, version)
                        if sub_dependencies:
                            process_dependencies(tx, dep_name, version, sub_dependencies)
                    else:
                        print("Root package and dep : ", root_package_name, dep_name)
                        add_or_update_package(tx, root_package_name, root_version)
                        add_or_update_package(tx, dep_name, version)
                        add_dependency(tx, root_package_name, root_version, dep_name, version, "INCOMPATIBLE, UPGRADE PACKAGE. NOT EXPLORING FURTHER")
                        # dependenciess = get_dependencies_from_pypi(root_package_name)
                        # print(dependenciess)
                        # print()
                        # print(dep_name, dep_version)
                        sys.exit(f"Change the packages' versions {root_package_name}/{dep_name}, currently incompatible")
                else:
                    add_or_update_package(tx, dep_name, dep_version)
                    add_dependency(tx, root_package_name, root_version, dep_name, dep_version, "DEPENDS_ON")

                    # Recursively process sub-dependencies
                    sub_dependencies = get_dependencies_from_pypi(dep_name, dep_version)
                    if sub_dependencies:
                        process_dependencies(tx, dep_name, dep_version, sub_dependencies)

    def find_existing_package(tx, package_name):
        result = tx.run("MATCH (p:Package) WHERE p.name STARTS WITH $name RETURN p", name=package_name)
        record = result.single()
        if record:
            return record['p']
        else:
            return None



    

  

    def get_package_versions_from_pypi(package_name):
        url = f"https://pypi.org/pypi/{package_name}/json"
        try:
            response = requests.get(url)
            response.raise_for_status()  # Raise error for bad requests
            data = response.json()
            versions = list(data["releases"].keys())  # Get all available versions
            return versions
        except requests.exceptions.RequestException as e:
            print(f"Error fetching package versions from PyPI: {e}")
            return []

    def parse_version(new_version_spec, existing_version, dep_name):
        # Helper function to convert version string to SpecifierSet
        def to_specifier_set(version):
            return SpecifierSet(version)
        
        def has_no_specifiers(version):
            return not any(op in version for op in ['<', '>', '!', '~'])

        if existing_version:
            # Convert both new_version_spec and existing_version to SpecifierSet
            if not has_no_specifiers(new_version_spec) and not has_no_specifiers(existing_version):

                new_version_set = to_specifier_set(new_version_spec)
                # print(new_version_set)
                existing_version_set = to_specifier_set(existing_version)
                # print(existing_version_set)
                # Fetch available versions from PyPI
                available_versions = get_package_versions_from_pypi(dep_name)

                # Filter versions based on specifiers
                valid_versions = [ver for ver in available_versions if Version(ver) in (new_version_set & existing_version_set)]
                
                # Check if there are any valid versions
                if valid_versions:
                    valid_versions.sort()
                    # print(dep_name, valid_versions, True)
                    # Return True and the list of valid versions
                    return True, f'>={valid_versions[0]}, <={valid_versions[-1]}'
                else:
                    print(dep_name, valid_versions, False)
                    # Return False and the new version spec
                    return False, new_version_spec
            elif has_no_specifiers(new_version_spec) and not has_no_specifiers(existing_version):
                existing_version_set = to_specifier_set(existing_version)

                # Fetch available versions from PyPI
                available_versions = get_package_versions_from_pypi(dep_name)
                
                # Filter versions based on specifiers
                valid_versions = [ver for ver in available_versions if Version(ver) in (existing_version_set)]
                if new_version_spec in valid_versions:
                    # print(dep_name, valid_versions, True)
                    return True, new_version_spec
                else:
                    print(dep_name, valid_versions, False)
                    return False, new_version_spec
            
            elif not has_no_specifiers(new_version_spec) and has_no_specifiers(existing_version):
                new_version_set = to_specifier_set(new_version_spec)

                # Fetch available versions from PyPI
                available_versions = get_package_versions_from_pypi(dep_name)

                # Filter versions based on specifiers
                valid_versions = [ver for ver in available_versions if Version(ver) in (new_version_set)]
                if existing_version in valid_versions:
                    return True, existing_version
                else:
                    return False, existing_version
                
        else:
                # If no existing version, consider it as valid
                return True, new_version_spec


    



    with driver.session() as session:
        session.execute_write(process_dependencies, package_name, version, dependency_versions)

# Neo4j connection details
load_dotenv()
uri = os.getenv("URI")
user = os.getenv("USER")
password = os.getenv("PASSWORD") #insert your password

# Connect to Neo4j
driver = GraphDatabase.driver(uri, auth=(user, password))

# Path to requirements.txt file
requirements_file = 'requirements.txt'

# Create graphs for each package in requirements.txt
with open(requirements_file, 'r') as f:
    packages = f.readlines()
    for package_info in packages:
        package_info = package_info.strip().split('==')
        if len(package_info) >= 2 and package_info[0] and package_info[1]:
            package_name = package_info[0]
            package_version = package_info[1]

            # Get dependencies and their versions for the package from PyPI API
            dependency_versions = get_dependencies_from_pypi(package_name, package_version)

            # Create graph for the package and its dependencies
            create_graph(package_name, package_version, dependency_versions, driver)
        else:
            if package_info and package_info != ['']:
                print(f"Invalid format in line: {package_info}")

requirements_file = 'requirements.txt'
requirements = read_requirements(requirements_file)
    
common_python_versions, incompatible_packages = find_compatible_python_version(requirements)
    
print(f'Compatible Python versions for all packages: {common_python_versions}')

def get_python_versions():
    url = "https://www.python.org/downloads/"
    response = requests.get(url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, "html.parser")
        versions = set()
        for version in soup.find_all("span", class_="release-number"):
            version_text = version.get_text().strip().replace("Python ", "")
            try:
                Version(version_text)
                versions.add(version_text)
            except InvalidVersion:
                continue
        return sorted(versions, key=lambda v: Version(v))
    return []
# Retrieve all available Python versions
available_python_versions = get_python_versions()

# Filter available versions based on common specifier
filtered_versions = [ver for ver in available_python_versions if Version(ver) in common_python_versions]

# Determine minimum and maximum versions
min_version = min(filtered_versions, key=Version)
max_version = max(filtered_versions, key=Version)

print(f"Minimum compatible Python version: {min_version}")
print(f"Maximum compatible Python version: {max_version}")


if incompatible_packages:
    print(f'These packages could not be processed: {incompatible_packages}')
    
target_python_version = input("Enter the target Python version you want to upgrade to (e.g., 3.8): ")
upgraded_dependencies = {}
    
for req in requirements:
        req_obj = Requirement(req)
        package = req_obj.name
        version = str(req_obj.specifier).split(',')[0][2:]
        if target_python_version not in get_python_compatibility(package, version):
            try:
                
                upgrade = (target_python_version > max_version)

                latest_compatible_version = get_latest_compatible_version(package, target_python_version, upgrade)
                if latest_compatible_version:
                    upgraded_dependencies[package] = latest_compatible_version
                else:
                    upgraded_dependencies[package] = "No compatible version found"
            except Exception as e:
                print(f"Failed to process {req}: {e}")
    
print("Upgrade the dependencies, like so:")
for package, version in upgraded_dependencies.items():
        print(f"{package}: {version}")

# Close the Neo4j connection
driver.close()


