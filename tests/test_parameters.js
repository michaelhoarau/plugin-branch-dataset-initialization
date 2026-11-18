const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let controller;
const form = fs.readFileSync(path.join(__dirname, '../resource/branch-parameters.html'), 'utf8');
assert.match(form, /<div class="control-group" ng-if="!config\.dry_run">\s*<label[^>]*for="branch-tables-overwrite"/);
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/branch-parameters.js'), 'utf8'), {
    angular: {
        module: () => ({controller: (name, definition) => { controller = definition[1]; }})
    }
});

(async function () {
    const data = {master_name: '<Master>', master_key: 'MASTER', choices: [
        {value: 'MASTER', label: 'main — Master'},
        {value: 'SIBLING', label: 'feature — Sibling'}
    ], warnings: []};
    let onDryRunChange;
    const scope = {config: {}, callPythonDo: () => Promise.resolve(data), $watch: (expression, callback) => {
        assert.equal(expression, 'config.dry_run');
        onDryRunChange = callback;
        callback(scope.config.dry_run);
    }};
    controller(scope);
    await Promise.resolve();
    assert.equal(scope.config.dry_run, true);
    assert.equal(scope.config.overwrite_existing, false);
    assert.equal(scope.masterName, '<Master>');
    assert.equal(scope.masterKey, 'MASTER');
    assert.equal(scope.loading, false);
    assert.equal(scope.config.branch_project_key, 'MASTER');

    scope.config.dry_run = false;
    scope.config.overwrite_existing = true;
    scope.config.branch_project_key = 'SIBLING';
    await scope.loadBranches();
    assert.equal(scope.config.branch_project_key, 'SIBLING');
    assert.equal(scope.config.dry_run, false);
    assert.equal(scope.config.overwrite_existing, true);

    scope.config.dry_run = true;
    onDryRunChange(true);
    assert.equal(scope.config.overwrite_existing, false);
    scope.config.dry_run = false;
    onDryRunChange(false);
    assert.equal(scope.config.overwrite_existing, false);

    scope.config.branch_project_key = 'DELETED';
    await scope.loadBranches();
    assert.equal(scope.config.branch_project_key, 'MASTER');

    scope.callPythonDo = () => Promise.resolve({...data, choices: [{value: 'SIBLING', label: 'Sibling'}]});
    await scope.loadBranches();
    assert.equal(scope.config.branch_project_key, null);

    scope.callPythonDo = () => Promise.resolve({...data, choices: []});
    await scope.loadBranches();
    assert.equal(scope.config.branch_project_key, null);
    assert.equal(scope.choices.length, 0);

    scope.callPythonDo = () => Promise.reject(new Error('Unavailable'));
    await scope.loadBranches();
    assert.equal(scope.masterKey, null);
    assert.equal(scope.loading, false);
    assert.ok(scope.error);
    console.log('Passed parameter form state checks: defaults, master label, refresh, stale selection, empty choices, and load failure.');
})().catch(error => { console.error(error); process.exitCode = 1; });
