
import isleconfig
import numpy as np
import scipy.stats
from insurancecontract import InsuranceContract
from reinsurancecontract import ReinsuranceContract
from riskmodel import RiskModel
import sys, pdb
import uuid
import numba as nb

if isleconfig.use_abce:
    from genericagentabce import GenericAgent
    #print("abce imported")
else:
    from genericagent import GenericAgent
    #print("abce not imported")

class MetaInsuranceOrg(GenericAgent):
    def init(self, simulation_parameters, agent_parameters):
        self.simulation = simulation_parameters['simulation']
        self.simulation_parameters = simulation_parameters
        self.contract_runtime_dist = scipy.stats.randint(simulation_parameters["mean_contract_runtime"] - \
                  simulation_parameters["contract_runtime_halfspread"], simulation_parameters["mean_contract_runtime"] \
                  + simulation_parameters["contract_runtime_halfspread"] + 1)
        self.default_contract_payment_period = simulation_parameters["default_contract_payment_period"]
        self.id = agent_parameters['id']
        self.cash = agent_parameters['initial_cash']
        self.premium = agent_parameters["norm_premium"]
        self.profit_target = agent_parameters['profit_target']
        self.acceptance_threshold = agent_parameters['initial_acceptance_threshold']  # 0.5
        self.acceptance_threshold_friction = agent_parameters['acceptance_threshold_friction']  # 0.9 #1.0 to switch off
        self.interest_rate = agent_parameters["interest_rate"]
        self.reinsurance_limit = agent_parameters["reinsurance_limit"]
        self.simulation_no_risk_categories = simulation_parameters["no_categories"]
        self.simulation_reinsurance_type = simulation_parameters["simulation_reinsurance_type"]
        
        rm_config = agent_parameters['riskmodel_config']
        self.riskmodel = RiskModel(damage_distribution=rm_config["damage_distribution"], \
                                     expire_immediately=rm_config["expire_immediately"], \
                                     cat_separation_distribution=rm_config["cat_separation_distribution"], \
                                     norm_premium=rm_config["norm_premium"], \
                                     category_number=rm_config["no_categories"], \
                                     init_average_exposure=rm_config["risk_value_mean"], \
                                     init_average_risk_factor=rm_config["risk_factor_mean"], \
                                     init_profit_estimate=rm_config["norm_profit_markup"], \
                                     margin_of_safety=rm_config["margin_of_safety"], \
                                     var_tail_prob=rm_config["var_tail_prob"], \
                                     inaccuracy=rm_config["inaccuracy_by_categ"])
        
        self.category_reinsurance = [None for i in range(self.simulation_no_risk_categories)]
        if self.simulation_reinsurance_type == 'non-proportional':
            self.np_reinsurance_deductible_fraction = simulation_parameters["default_non-proportional_reinsurance_deductible"]
            self.np_reinsurance_excess_fraction = simulation_parameters["default_non-proportional_reinsurance_excess"]
            self.np_reinsurance_premium_share = simulation_parameters["default_non-proportional_reinsurance_premium_share"]
        self.obligations = []
        self.underwritten_contracts = []
        #self.reinsurance_contracts = []
        self.operational = True
        self.is_insurer = True
        self.is_reinsurer = False

    def iterate(self, time):        # TODO: split function so that only the sequence of events remains here and everything else is in separate methods
        """obtain investments yield"""
        self.obtain_yield(time)

        """realize due payments"""
        self.effect_payments(time)
        print(time, ":", self.id, len(self.underwritten_contracts), self.cash, self.operational)

        self.make_reinsurance_claims(time)

        """mature contracts"""
        print("Number of underwritten contracts ", len(self.underwritten_contracts))
        maturing = [contract for contract in self.underwritten_contracts if contract.expiration <= time]
        for contract in maturing:
            self.underwritten_contracts.remove(contract)
            contract.mature(time)
        contracts_dissolved = len(maturing)

        """effect payments from contracts"""
        [contract.check_payment_due(time) for contract in self.underwritten_contracts]

        if self.operational:

            """request risks to be considered for underwriting in the next period and collect those for this period"""
            new_risks = []
            if self.is_insurer:
                new_risks += self.simulation.solicit_insurance_requests(self.id, self.cash)
            if self.is_reinsurer:
                new_risks += self.simulation.solicit_reinsurance_requests(self.id, self.cash)
            contracts_offered = len(new_risks)
            try:
                assert contracts_offered > 2 * contracts_dissolved
            except:
                print("Something wrong; agent {0:d} receives too few new contracts {1:d} <= {2:d}".format(self.id, contracts_offered, 2*contracts_dissolved))
            #print(self.id, " has ", len(self.underwritten_contracts), " & receives ", contracts_offered, " & lost ", contracts_dissolved)
            
            new_nonproportional_risks = [risk for risk in new_risks if risk.get("insurancetype")=='excess-of-loss' and risk["owner"] is not self]
            new_risks = [risk for risk in new_risks if risk.get("insurancetype") in ['proportional', None] and risk["owner"] is not self]

            underwritten_risks = [{"value": contract.value, "category": contract.category, \
                            "risk_factor": contract.risk_factor, "deductible": contract.deductible, \
                            "excess": contract.excess, "insurancetype": contract.insurancetype, \
                            "runtime": contract.runtime} for contract in self.underwritten_contracts if contract.reinsurance_share != 1.0]
            
            """deal with non-proportional risks first as they must evaluate each request separatly, then with proportional ones"""
            for risk in new_nonproportional_risks:
                accept = self.riskmodel.evaluate(underwritten_risks, self.cash, risk)       # TODO: change riskmodel.evaluate() to accept new risk to be evaluated and to account for existing non-proportional risks correctly -> DONE.
                if accept:
                    per_value_reinsurance_premium = self.np_reinsurance_premium_share * risk["periodized_total_premium"] * risk["runtime"] / risk["value"]            #TODO: rename this to per_value_premium in insurancecontract.py to avoid confusion
                    contract = ReinsuranceContract(self, risk, time, per_value_reinsurance_premium, risk["runtime"], \
                                                  self.default_contract_payment_period, \
                                                  expire_immediately=self.simulation_parameters["expire_immediately"], \
                                                  insurancetype=risk["insurancetype"])        # TODO: implement excess of loss for reinsurance contracts
                    self.underwritten_contracts.append(contract)
                #pass    # TODO: write this nonproportional risk acceptance decision section based on commented code in the lines above this -> DONE.
            
            """make underwriting decisions, category-wise"""
            # TODO: Enable reinsurance shares other tan 0.0 and 1.0
            expected_profit, acceptable_by_category = self.riskmodel.evaluate(underwritten_risks, self.cash)

            #if expected_profit * 1./self.cash < self.profit_target:
            #    self.acceptance_threshold = ((self.acceptance_threshold - .4) * 5. * self.acceptance_threshold_friction) / 5. + .4
            #else:
            #    self.acceptance_threshold = (1 - self.acceptance_threshold_friction * (1 - (self.acceptance_threshold - .4) * 5.)) / 5. + .4

            growth_limit = max(50, 2 * len(self.underwritten_contracts) + contracts_dissolved)
            if sum(acceptable_by_category) > growth_limit:
                acceptable_by_category = np.asarray(acceptable_by_category)
                acceptable_by_category = acceptable_by_category * growth_limit / sum(acceptable_by_category)
                acceptable_by_category = np.int64(np.round(acceptable_by_category))

            not_accepted_risks = []
            for categ_id in range(len(acceptable_by_category)):
                categ_risks = [risk for risk in new_risks if risk["category"] == categ_id]
                new_risks = [risk for risk in new_risks if risk["category"] != categ_id]
                categ_risks = sorted(categ_risks, key = lambda risk: risk["risk_factor"])
                i = 0
                print("InsuranceFirm underwrote: ", len(self.underwritten_contracts), " will accept: ", acceptable_by_category[categ_id], " out of ", len(categ_risks), "acceptance threshold: ", self.acceptance_threshold)
                while (acceptable_by_category[categ_id] > 0 and len(categ_risks) > i): #\
                    #and categ_risks[i]["risk_factor"] < self.acceptance_threshold):
                    if categ_risks[i].get("contract") is not None: #categ_risks[i]["reinsurance"]:
                        if categ_risks[i]["contract"].expiration > time:    # required to rule out contracts that have exploded in the meantime
                            #print("ACCEPTING", categ_risks[i]["contract"].expiration, categ_risks[i]["expiration"], categ_risks[i]["identifier"], categ_risks[i].get("contract").terminating)
                            contract = ReinsuranceContract(self, categ_risks[i], time, \
                                          self.simulation.get_market_premium(), categ_risks[i]["expiration"] - time, \
                                          self.default_contract_payment_period, \
                                          expire_immediately=self.simulation_parameters["expire_immediately"], )  
                            self.underwritten_contracts.append(contract)
                            #categ_risks[i]["contract"].reincontract = contract
                            # TODO: move this to insurancecontract (ca. line 14) -> DONE
                            # TODO: do not write into other object's properties, use setter -> DONE

                            assert categ_risks[i]["contract"].expiration >= contract.expiration, "Reinsurancecontract lasts longer than insurancecontract: {0:d}>{1:d} (EXPIRATION2: {2:d} Time: {3:d})".format(contract.expiration, categ_risks[i]["contract"].expiration, categ_risks[i]["expiration"], time)
                        #else:
                        #    pass
                    else:
                        contract = InsuranceContract(self, categ_risks[i], time, self.simulation.get_market_premium(), \
                                                     self.contract_runtime_dist.rvs(), \
                                                     self.default_contract_payment_period, \
                                                     expire_immediately=self.simulation_parameters["expire_immediately"])
                        self.underwritten_contracts.append(contract)
                    acceptable_by_category[categ_id] -= 1   # TODO: allow different values per risk (i.e. sum over value (and reinsurance_share) or exposure instead of counting)
                    i += 1

                not_accepted_risks += categ_risks[i:]
                not_accepted_risks = [risk for risk in not_accepted_risks if risk.get("contract") is None]

            # seek reinsurance
            if self.is_insurer:
                # TODO: Why should only insurers be able to get reinsurance (not reinsurers)? (Technically, it should work)
                self.ask_reinsurance(time)

            # return unacceptables
            #print(self.id, " now has ", len(self.underwritten_contracts), " & returns ", len(not_accepted_risks))
            self.simulation.return_risks(not_accepted_risks)

            #not implemented
            #"""adjust liquidity, borrow or invest"""
            #pass

    def enter_illiquidity(self, time):
        self.enter_bankruptcy(time)

    def enter_bankruptcy(self, time):
        [contract.dissolve(time) for contract in self.underwritten_contracts]   # removing (dissolving) all risks immediately after bankruptcy (may not be realistic, they might instead be bought by another company)
        self.simulation.receive(self.cash)
        self.cash = 0
        self.operational = False

    def receive_obligation(self, amount, recipient, due_time):
        obligation = {"amount": amount, "recipient": recipient, "due_time": due_time}
        self.obligations.append(obligation)

    def effect_payments(self, time):
        due = [item for item in self.obligations if item["due_time"]<=time]
        self.obligations = [item for item in self.obligations if item["due_time"]>time]
        sum_due = sum([item["amount"] for item in due])
        if sum_due > self.cash:
            self.obligations += due
            self.enter_illiquidity(time)
        else:
            for obligation in due:
                self.pay(obligation["amount"], obligation["recipient"])


    def pay(self, amount, recipient):
        self.cash -= amount
        recipient.receive(amount)

    def receive(self, amount):
        """Method to accept cash payments."""
        self.cash += amount

    def obtain_yield(self, time):
        amount = self.cash * self.interest_rate
        self.simulation.receive_obligation(amount, self, time)
    
    def ask_reinsurance(self, time):
        if self.simulation_reinsurance_type == 'proportional':
            self.ask_reinsurance_proportional()
        elif self.simulation_reinsurance_type == 'non-proportional':
            self.ask_reinsurance_non_proportional(time)
        else:
            assert False, "Undefined reinsurance type"

    @nb.jit
    def ask_reinsurance_non_proportional(self, time):
        for categ_id in range(self.simulation_no_risk_categories):
            # with probability 5% if not reinsured ...      # TODO: find a more generic way to decide whether to request reinsurance for category in this period
            if (self.category_reinsurance[categ_id] is None) and np.random.random() < 0.1:
                total_value = 0
                avg_risk_factor = 0
                number_risks = 0
                periodized_total_premium = 0
                for contract in self.underwritten_contracts:
                    if contract.category == categ_id:
                        total_value += contract.value
                        avg_risk_factor += contract.risk_factor
                        number_risks += 1
                        periodized_total_premium += contract.periodized_premium
                avg_risk_factor /= number_risks
                risk = {"value": total_value, "category": categ_id, "owner": self,
                            #"identifier": uuid.uuid1(),
                            "insurancetype": 'excess-of-loss', "number_risks": number_risks, 
                            "deductible_fraction": self.np_reinsurance_deductible_fraction, 
                            "excess_fraction": self.np_reinsurance_excess_fraction,
                            "periodized_total_premium": periodized_total_premium, "runtime": 12,
                            "expiration": time + 12, "risk_factor": avg_risk_factor}    # TODO: make runtime into a parameter

                self.simulation.append_reinrisks(risk)

    @nb.jit
    def ask_reinsurance_proportional(self):
        nonreinsured = []
        for contract in self.underwritten_contracts:
            if contract.reincontract == None:
                nonreinsured.append(contract)

        #nonreinsured_b = [contract
        #                for contract in self.underwritten_contracts
        #                if contract.reincontract == None]
        #
        #try:
        #    assert nonreinsured == nonreinsured_b
        #except:
        #    pdb.set_trace()

        nonreinsured.reverse()

        if len(nonreinsured) >= (1 - self.reinsurance_limit) * len(self.underwritten_contracts):
            counter = 0
            limitrein = len(nonreinsured) - (1 - self.reinsurance_limit) * len(self.underwritten_contracts)
            for contract in nonreinsured:
                if counter < limitrein:
                    risk = {"value": contract.value, "category": contract.category, "owner": self,
                            #"identifier": uuid.uuid1(),
                            "reinsurance_share": 1.,
                            "expiration": contract.expiration, "contract": contract,
                            "risk_factor": contract.risk_factor}

                    #print("CREATING", risk["expiration"], contract.expiration, risk["contract"].expiration, risk["identifier"])
                    self.simulation.append_reinrisks(risk)
                    counter += 1
                else:
                    break

    def add_reinsurance(self, category, excess_fraction, deductible_fraction, contract):
        self.riskmodel.add_reinsurance(category, excess_fraction, deductible_fraction, contract)
        self.category_reinsurance[category] = contract
        #pass

    def delete_reinsurance(self, category, excess_fraction, deductible_fraction, contract):
        self.riskmodel.delete_reinsurance(category, excess_fraction, deductible_fraction, contract)
        self.category_reinsurance[category] = None
        #pass

    def get_cash(self):
        return self.cash

    def logme(self):
        self.log('cash', self.cash)
        self.log('underwritten_contracts', self.underwritten_contracts)
        self.log('operational', self.operational)

    #def zeros(self):
    #    return 0

    def len_underwritten_contracts(self):
        return len(self.underwritten_contracts)

    def get_operational(self):
        return self.operational

    def get_underwritten_contracts(self):
        return self.underwritten_contracts
    
    def get_pointer(self):
        return self

    def make_reinsurance_claims(self,time):
        """collect and effect reinsurance claims"""
        # TODO: reorganize this with risk category ledgers
        # TODO: Put facultative insurance claims here
        claims_this_turn = np.zeros(self.simulation_no_risk_categories)
        for contract in self.underwritten_contracts:
            categ_id, claims, is_proportional = contract.get_and_reset_current_claim()
            if is_proportional:
                claims_this_turn[categ_id] += claims
            if (contract.reincontract != None):
                contract.reincontract.explode(time, claims)

        for categ_id in range(self.simulation_no_risk_categories):
            if claims_this_turn[categ_id] > 0 and self.category_reinsurance[categ_id] is not None:
                self.category_reinsurance[categ_id].explode(time, claims_this_turn[categ_id])